from app.main import app, db
from app.model import ScpusFeed, ScpusRequest
from app.business import count_results_for_query, get_results_for_query, update_feed, generate_rss
from flask import abort, Response, render_template, request

import pickle


@app.route("/robots.txt")
def block_robots():
    return """User-agent: *    
Disallow: /"""


@app.route("/feeds")
def list_feeds():
    feeds = db.session.query(ScpusFeed).all()
    return render_template('feeds.html', feeds=feeds)


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
        dois = get_results_for_query(count, feed.query)
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


@app.route('/')
@app.route('/home')
def home():
    return render_template('index.html')


@app.route('/history', methods=["GET"])
def history():
    queries = db.session.query(ScpusRequest).all()
    return render_template('history.html', queries=queries)


@app.route('/snowball', methods=["GET"])
def snowball():
    title = request.args.get('title')

    return render_template('index.html', query=f"REFTITLE(\"{title}\")")


@app.route('/sameauthor', methods=["GET"])
def same_author():
    name = request.args.get('name')

    return render_template('index.html', query=f"AUTHOR-NAME({name})")


@app.route('/permalink', methods=["GET"])
def permalink():
    query = request.args.get('query')

    return render_template('index.html', query=f"{query}")
