from flask import Flask, render_template, request
import requests
import itertools
import os
import urllib
from flask_socketio import SocketIO, emit, send
import re
import datetime

from flask_sqlalchemy import SQLAlchemy


class Config(object):
    SQLALCHEMY_DATABASE_URI = os.environ.get("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory")
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
app.config['SECRET_KEY'] = 'secret!'
print(f"Using API_KEY={API_KEY}")


class ScpusRequest(db.Model):
    __tablename = "history"
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(2048))
    ip = db.Column(db.String(64), default="0.0.0.0")
    count = db.Column(db.Integer, default=-1)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    fetched = db.Column(db.Boolean)


db.create_all()


@app.route("/robots.txt")
def block_robots():
    return """User-agent: *    
Disallow: /"""


@app.route('/')
@app.route('/home')
def home():
    return render_template('index.html')


@app.route('/history',methods=["GET"])
def history():
    queries=db.session.query(ScpusRequest).limit(100)
    return render_template('history.html',queries=queries)


@app.route('/snowball', methods=["GET"])
def query():
    title = request.args.get('title')

    return render_template('index.html', query=f"REFTITLE(\"{title}\")")


def get_results_for_query(count, query):
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
            bucket.append({"doi": aa.get("prism:doi", ""), "title": aa.get("dc:title", "-"), "year": year,
                           "pubtitle": aa.get('prism:publicationName', "")});
            emit('doi_update', {"total": count, "done": success, "failed": failed})
        emit('doi_results', bucket)
        dois = dois + bucket
    emit('doi_export_done', dois)
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
    dois = get_results_for_query(count, the_query)

    emit("dois", {"dois": dois})


if __name__ == '__main__':
    pass
