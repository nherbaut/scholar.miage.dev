from flask_socketio import emit
from typing import Dict, Iterable, List, Set, Tuple
from app.main import socketio, db
from app.business import count_results_for_query, get_scopus_works_for_query, create_short_link, net_build_graph
from app.model import ScpusFeed, ScpusRequest, NetworkData
from app.researchers import get_venue_for_orcid, get_venue_for_openalex
import json
import pickle
from collections import Counter


@socketio.on('create_network_graph_data')
def net_create_graph_data(json_data):

    def network_emit(nework_report):
        emit("nework_report",  nework_report)

    result = net_build_graph(json_data["ids"], 2, emitt=network_emit)
    graph_data = NetworkData(network_data=pickle.dumps(json.dumps(result)))
    db.session.add(graph_data)
    db.session.commit()

    emit("nework_report_done", {"network_id": graph_data.id})


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


@socketio.on('create_permalink')
def handle_count(json_data, log_query=False):
    query = json_data["query"]
    short_url = create_short_link(query)
    emit('permalink_generated', short_url)


@socketio.on('count')
def handle_count(json_data, log_query=False):
    count = count_results_for_query(json_data["query"])

    n = ScpusRequest(query=json_data["query"],
                     ip="0.0.0.0", count=count, fetched=False)
    db.session.add(n)
    db.session.commit()

    emit("count", count)

#


@socketio.on("get_venue_openalex")
def handle_get_venues(openalex_id):
    def venue_emit(venue):
        emit("venue_update",  venue)

    def author_emit(author_name):
        emit("author_name",  author_name)
    venues = dict(Counter(get_venue_for_openalex(
        openalex_id, venue_emit, author_emit)))
    emit("venues", json.dumps(venues))


@socketio.on("get_venue")
def handle_get_venues(orcid):
    def venue_emit(venue):
        emit("venue_update",  venue)

    def author_emit(author_name):
        emit("author_name",  author_name)
    venues = dict(Counter(get_venue_for_orcid(orcid, venue_emit, author_emit)))
    emit("venues", json.dumps(venues))


@socketio.on('get_dois')
def handle_get_dois(json_data):
    the_query = json_data["query"]
    xref = json_data["xref"]
    count = count_results_for_query(the_query)
    dois = get_scopus_works_for_query(count, the_query, xref=xref, emitt=emit)

    emit("dois", {"dois": dois})
