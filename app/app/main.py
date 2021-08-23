from flask import Flask, render_template, request, Response, abort
import requests
import itertools
import os
import urllib
from flask_socketio import SocketIO, emit, send
import re
import datetime
import pickle
import dateparser
from flask_sqlalchemy import SQLAlchemy
from feedgen.feed import FeedGenerator
import pytz

DEFAULT_DB = "sqlite:///memory"


class Config(object):
    SQLALCHEMY_DATABASE_URI = os.environ.get("SQLALCHEMY_DATABASE_URI", DEFAULT_DB)
    if SQLALCHEMY_DATABASE_URI == DEFAULT_DB:
        IN_MEMORY = True
    else:
        IN_MEMORY = False
    DEVELOPMENT = False
    DEBUG = False  # some Flask specific configs
    SECRET_KEY = 'EMNS2606!'
    SQLALCHEMY_ECHO = False
    SQLALCHEMY_TRACK_MODIFICATIONS = False


app = Flask(__name__)
API_KEY = os.environ["API_KEY"]
SCPUS_BACKEND = f'https://api.elsevier.com/content/search/scopus?start=%d&count=%d&query=%s&apiKey={API_KEY}'
db = None
app.config.from_object(Config())

socketio = SocketIO(app)
socketio.init_app(app, cors_allowed_origins="*")
socketio.run(app, host="0.0.0.0")
db = SQLAlchemy(app)


class ScpusRequest(db.Model):
    __tablename = "history"
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(2048))
    ip = db.Column(db.String(64), default="0.0.0.0")
    count = db.Column(db.Integer, default=-1)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    fetched = db.Column(db.Boolean)


class ScpusFeed(db.Model):
    __tablename = "feed"
    id = db.Column(db.Integer, primary_key=True)
    feed_content = db.Column(db.Text, default=None)
    count = db.Column(db.Integer)
    query = db.Column(db.String(2048))
    lastBuildDate = db.Column(db.DateTime, default=datetime.datetime.utcnow)


if Config().IN_MEMORY:
    db.create_all()
    db.session.commit()
app.config['SECRET_KEY'] = 'secret!'
print(f"Using API_KEY={API_KEY}")


@app.route("/robots.txt")
def block_robots():
    return """User-agent: *    
Disallow: /"""


@app.route("/feed/<id>.rss")
def get_feed(id):
    try:
        feed = db.session.query(ScpusFeed).filter(ScpusFeed.id == id).one()
    except db.orm.exc.NoResultFound as e:
        return abort(404, description="No feed with this id")

    count = count_results_for_query(feed.query)
    if count != feed.count:
        dois = get_results_for_query(count, feed.query)

    if feed.feed_content is not None:
        feed_content = pickle.loads(feed.feed_content)
    else:
        feed_content = {}

    update_feed(dois, feed_content)

    feed_items = sorted(feed_content.values(), key=lambda x: x["x-added-on"],reverse=True)

    rss = generate_rss(feed_items,feed.id,feed.query)

    return Response(rss, mimetype='application/rss+xml')


def generate_rss(feed_items, id="id", query="query"):
    fg = FeedGenerator()
    for item in feed_items:
        fe = fg.add_entry()
        for key, value in item.items():
            if key.startswith("x-"):
                continue
            setter = getattr(fe, key)

            if isinstance(value, list):
                for value_item in value:
                    setter(value_item)
            else:
                setter(value)
    fg.title(f"Bibliography Feed {id}")
    fg.link({"href": 'https://scpushack.nextnet.top', "rel": 'alternate'})
    fg.description(f"results for query: {query}")
    return fg.rss_str()


def update_feed(dois, feed_content):
    for item in dois:
        if item["doi"] not in feed_content:
            doi = item["doi"]
            feed_content[item["doi"]] = {"content": "https://doi.org/" + doi,
                                         "link": [{"href": "https://doi.org/" + doi,
                                                   "rel": "alternate",
                                                   "title": "publisher's site"},
                                                  {"href": "https://scpushack.nextnet.top",
                                                   "rel": "via",
                                                   "title": "Authoring search engine"},
                                                  {"href": "https://sci-hub.se/" + doi,
                                                   "rel": "related",
                                                   "title": "SciHub link"}
                                                  ],
                                         "title": item["title"],
                                         "pubdate": item["X-coverDate"],
                                         "title": item["title"],
                                         "x-added-on": datetime.datetime.utcnow(),
                                         "description": f"published in {item['year']} by {item['pubtitle']} try to access it on <a href='{'https://sci-hub.se/' + doi}'>scihub here</a>"}


@socketio.on('create_feed')
def create_feed(json_data):
    feed = ScpusFeed(query=json_data["query"])

    db.session.add(feed)
    db.session.commit()

    emit("feed_generated", {"feed_id": feed.id})


@app.route('/')
@app.route('/home')
def home():
    return render_template('index.html')


@app.route('/history', methods=["GET"])
def history():
    queries = db.session.query(ScpusRequest).limit(100)
    return render_template('history.html', queries=queries)


@app.route('/snowball', methods=["GET"])
def query():
    title = request.args.get('title')

    return render_template('index.html', query=f"REFTITLE(\"{title}\")")


def get_results_for_query(count, query, emitt=lambda *args, **kwargs: None):
    dois = []

    failed = 0
    success = 0
    for i in range(0, min(1000, count), 25):
        bucket = []
        partial_results = requests.get(
            SCPUS_BACKEND % (i, 25, query.replace(" ", "+").replace("\\", "%%22"))).json()
        for aa in partial_results["search-results"]["entry"]:
            if "prism:doi" in aa:
                success += 1
            else:
                failed += 1

            year = aa.get('prism:coverDisplayDate', "")
            if year != "":
                rematch = re.findall("[0-9]{4}", year)
                if len(rematch) > 0:
                    year = rematch[0]
            coverDate = aa.get("prism:coverDate", "")
            if coverDate == "":
                coverDate = datetime.datetime.utcnow()
            else:
                coverDate = dateparser.parse(coverDate)

            coverDate = pytz.timezone("UTC").localize(coverDate)
            bucket.append({"doi": aa.get("prism:doi", ""), "title": aa.get("dc:title", "-"), "year": year,
                           "pubtitle": aa.get('prism:publicationName', ""), "X-coverDate": str(coverDate)});
            emitt('doi_update', {"total": count, "done": success, "failed": failed})
        emitt('doi_results', bucket)
        dois = dois + bucket
    emitt('doi_export_done', dois)
    return dois

def count_results_for_query(query):
    print(f"query with {API_KEY} API_KEY")
    is_count = request.form.get("count")

    count = int(
        requests.get(SCPUS_BACKEND % (0, 1, query.replace(" ", "+").replace("\\", "%%22"))).json()["search-results"][
            "opensearch:totalResults"])
    return count


@socketio.on('my event')
def handle_message(data):
    for i in range(0, 10000):
        emit('news', i)


@socketio.on('count')
def handle_count(json_data):
    count = count_results_for_query(json_data["query"])

    n = ScpusRequest(query=json_data["query"], ip="0.0.0.0", count=count, fetched=False)

    db.session.add(n)
    db.session.commit()

    emit("count", count)


@socketio.on('get_dois')
def handle_get_dois(json_data):
    the_query = json_data["query"]
    count = count_results_for_query(the_query)
    dois = get_results_for_query(count, the_query, emitt=emit)

    emit("dois", {"dois": dois})


if __name__ == '__main__':
    pass
