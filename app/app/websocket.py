from flask_socketio import emit

from app.main import socketio, db
from app.business import count_results_for_query, get_results_for_query
from app.model import ScpusFeed, ScpusRequest




@socketio.on('create_feed')
def create_feed(json_data):
    feed = ScpusFeed(query=json_data["query"])

    db.session.add(feed)
    db.session.commit()

    emit("feed_generated", {"feed_id": feed.id})


@socketio.on('my event')
def handle_message(data):
    for i in range(0, 10000):
        emit('news', i)


@socketio.on('count')
def handle_count(json_data,log_query=False):
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
