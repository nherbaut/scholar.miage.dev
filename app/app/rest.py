import requests

from app.main import app, db
from app.model import ScpusFeed, ScpusRequest, PublicationSource
from app.business import count_results_for_query, get_results_for_query, update_feed, generate_rss, get_sources
from flask import abort, Response, render_template, request, session, redirect, url_for, send_from_directory
# from mendeley import Mendeley
# from mendeley.session import MendeleySession
# from mendeley.exception import MendeleyException, MendeleyApiException
import json
import pickle
import os


# mendeley = Mendeley(MENDELEY_CLIENT_ID, MENDELEY_SECRET, redirect_uri="http://localhost:5000/oauth")


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static/img'),
                               'ms.ico', mimetype='image/vnd.microsoft.icon')


@app.route("/robots.txt")
def block_robots():
    return """User-agent: *    
Disallow: /"""


@app.route("/sources", methods=["GET"])
def list_sources():
    sources = [{"short_name": ps.short_name, "full_text_name": ps.full_text_name, "code": ps.code} for ps in
               db.session.query(PublicationSource).all()]
    response = app.response_class(
        response=json.dumps(sources),
        status=200,
        mimetype='application/json'
    )
    return response


@app.route("/source/<short_name>", methods=["DELETE"])
def delete_conference(short_name):
    try:
        conf = db.session.query(PublicationSource).filter(PublicationSource.short_name == short_name).one()
        db.session.delete(conf)
        db.session.commit()
        return "DELETED", 204
    except Exception as e:
        return abort(404, description="No conference with this short name")


@app.route("/source", methods=["POST"])
def add_journal():
    journal = request.get_json()
    if db.session.query(PublicationSource).filter(
            PublicationSource.short_name == journal['short_name']).count() != 0:
        return abort(409, description="journal already exist")
    else:
        source = PublicationSource(short_name=journal['short_name'], full_text_name=journal['full_text_name'],
                                   code=journal['code'], category=journal['category'])
        db.session.add(source)
        db.session.commit()
        return "CREATED", 204


@app.route("/feeds")
def list_feeds():
    feeds = db.session.query(ScpusFeed).all()
    return render_template('feeds.html', feeds=feeds)


@app.route("/feed/<id>.rss", methods=["DELETE"])
def remove_rss(id):
    try:
        feed = db.session.query(ScpusFeed).filter(ScpusFeed.id == id).one()
        db.session.delete(feed)
        db.session.commit()
    except db.orm.exc.NoResultFound as e:
        return abort(404, description="No feed with this id")
    db.session.commit()
    return "DELETED", 204


@app.route("/feed/<id>.rss/items", methods=["DELETE"])
def purge_items(id):
    try:
        feed = db.session.query(ScpusFeed).filter(ScpusFeed.id == id).one()
    except db.orm.exc.NoResultFound as e:
        return abort(404, description="No feed with this id")
    feed.feed_content = None
    feed.count = -1
    db.session.commit()
    return "DELETED", 204


@app.route("/feed/<id>.rss")
def get_feed(id):
    try:
        feed = db.session.query(ScpusFeed).filter(ScpusFeed.id == id).one()
    except db.orm.exc.NoResultFound as e:
        return abort(404, description="No feed with this id")

    count = count_results_for_query(feed.query)
    if count != feed.count:
        dois = get_results_for_query(count, feed.query, xref=False)
    else:
        dois = []
    if feed.feed_content is not None:
        feed_content = pickle.loads(feed.feed_content)
    else:
        feed_content = {}

    update_feed(dois, feed_content)
    feed.count = count
    feed.feed_content = pickle.dumps(feed_content)
    feed.hit += 1
    db.session.commit()

    feed_items = sorted(feed_content.values(), key=lambda x: x["x-added-on"], reverse=True)

    rss = generate_rss(feed_items, feed.id, feed.query)

    return Response(rss, mimetype='application/rss+xml')


# @app.route("/mendeleyLogout")
# def mendeleyLogout():
#    session.clear()
#    return redirect('/')


# @app.route('/oauth')
# def auth_return():
#    auth = mendeley.start_authorization_code_flow(state=session['state'])
# mendeley_session = auth.authenticate(request.url)
#
# session.clear()
# session['token'] = mendeley_session.token
#
# return redirect('/')


# def get_session_from_cookies():
#    return MendeleySession(mendeley, session['token'])


@app.route('/')
@app.route('/home')
def home():
    # if 'token' in session:
    #        return render_template('index.html', token=session['token'])
    # else:
    #        auth = mendeley.start_authorization_code_flow()
    #        session['state'] = auth.state
    #    return render_template('index.html', login_url=auth.get_login_url())
    return render_template('index.html', sources=get_sources())


@app.route("/cite", methods=["GET"])
def cite():
    doi = request.args.get('doi')
    style = request.args.get('style')
    if style == "bibtex":
        accept = "application/x-bibtex"
        headers = {'Accept': f"{accept}"}
    else:
        style = "apa" if style is None else style
        accept = "text/x-bibliography"
        headers = {'Accept': f"{accept}; style={style}"}


    resp = requests.get(f"https://doi.org/{doi}", headers=headers)
    if resp.status_code == 200:
        return app.response_class(
            response=json.dumps({"doi": doi, "citation": resp.content.decode("utf-8")}),
            status=200,
            mimetype='application/json'
        )

    else:
        abort(resp.status_code)


@app.route('/history', methods=["GET"])
def history():
    limit = request.args.get('limit')
    if limit is None:
        limit = 2000
    queries = db.session.query(ScpusRequest).order_by(ScpusRequest.timestamp.desc()).limit(limit).all()
    accepts = request.headers["Accept"].split(",")
    if "application/json" in accepts:
        return app.response_class(
            response=json.dumps([q.query for q in queries]),
            status=200,
            mimetype='application/json'
        )

    else:
        return render_template('history.html', queries=queries)


@app.route('/snowball', methods=["GET"])
def snowball():
    title = request.args.get('title')

    return render_template('index.html', query=f"REFTITLE(\"{title}\")", sources=get_sources())


@app.route('/sameauthor', methods=["GET"])
def same_author():
    name = request.args.get('name')
    orcid = request.args.get('orcid')
    if orcid:
        return render_template('index.html', query=f"ORCID({orcid})", sources=get_sources())
    else:
        return render_template('index.html', query=f"AUTHOR-NAME({name})", sources=get_sources())


@app.route('/permalink', methods=["GET"])
def permalink():
    query = request.args.get('query')

    return render_template('index.html', query=f"{query}", sources=get_sources())


@app.route('/opensearch', methods=["GET"])
def opensearch():
    query = request.args.get('query')

    return render_template('index.html', query=f"{query}", sources=get_sources())
