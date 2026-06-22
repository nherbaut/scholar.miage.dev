from flask_socketio import emit
from typing import Dict, Iterable, List, Set, Tuple
from app.main import socketio, db
from app.business import count_results_for_query, get_papers, net_build_graph
from app.model import ScpusFeed, ScpusRequest, NetworkData
from app.researchers import get_venue_for_orcid, get_venue_for_openalex
import json
import pickle
import logging
import time
from collections import Counter

logger = logging.getLogger(__name__)


@socketio.on('create_network_graph_data')
def net_create_graph_data(json_data):

    def network_emit(nework_report):
        emit("nework_report",  nework_report)

    result = net_build_graph(json_data["ids"], 2, emitt=network_emit)
    graph_data = NetworkData(
        query=json_data["query"], network_data=pickle.dumps(json.dumps(result)))
    db.session.add(graph_data)
    db.session.commit()

    emit("nework_report_done", {"network_id": graph_data.id})


@socketio.on('create_feed')
def create_feed(json_data):
    feed = ScpusFeed(query=json_data["query"])

    db.session.add(feed)
    db.session.commit()

    emit("feed_generated", {"feed_id": feed.id})


@socketio.on('count')
def handle_count(json_data, log_query=False):
    started_at = time.monotonic()
    include_arxiv = json_data["arxiv"]
    query = json_data["query"]
    logger.info(
        "socket count start arxiv=%s query_len=%s query=%r",
        include_arxiv,
        len(query or ""),
        query,
    )
    
    if include_arxiv:
        def arxiv_warning(message: str):
            logger.warning("socket count arxiv warning message=%r query=%r", message, query)
            emit("arxiv_warning", {"message": message})
    else:
        def arxiv_warning(message: str):
            pass

    count_scopus, count_arxiv = count_results_for_query(
        query,
        include_arxiv=include_arxiv,
        arxiv_warning=arxiv_warning,
    )
    logger.info(
        "socket count computed scopus=%s arxiv=%s total=%s duration=%.2fs query=%r",
        count_scopus,
        count_arxiv,
        count_scopus + count_arxiv,
        time.monotonic() - started_at,
        query,
    )

    n = ScpusRequest(query=query,
                     ip="0.0.0.0", count=count_scopus + count_arxiv, fetched=False)
    db.session.add(n)
    db.session.commit()
    logger.info("socket count saved query_id=%s query=%r", n.id, query)

    emit("query_id", n.id)

    emit("count", count_scopus + count_arxiv)
    logger.info("socket count emitted total=%s duration=%.2fs query=%r", count_scopus + count_arxiv, time.monotonic() - started_at, query)


@socketio.on("get_venue_openalex")
def get_venue_openalex(openalex_id):
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
    started_at = time.monotonic()
    the_query = json_data["query"]
    xref = json_data["xref"]
    arxiv = json_data["arxiv"]
    logger.info(
        "socket get_dois start xref=%s arxiv=%s query_len=%s query=%r",
        xref,
        arxiv,
        len(the_query or ""),
        the_query,
    )

    if arxiv:
        def arxiv_warning(message: str):
            logger.warning("socket get_dois arxiv warning message=%r query=%r", message, the_query)
            emit("arxiv_warning", {"message": message})
    else:
        def arxiv_warning(message: str):
            pass

    count_scopus, count_arxiv = count_results_for_query(
        the_query,
        include_arxiv=arxiv,
        arxiv_warning=arxiv_warning,
    )
    logger.info(
        "socket get_dois count done scopus=%s arxiv=%s total=%s duration=%.2fs query=%r",
        count_scopus,
        count_arxiv,
        count_scopus + count_arxiv,
        time.monotonic() - started_at,
        the_query,
    )
    dois = get_papers(count_scopus, the_query, xref=xref,
                      arxiv=arxiv, emitt=emit, count_arxiv=count_arxiv,
                      arxiv_warning=arxiv_warning)
    logger.info(
        "socket get_dois done returned=%s duration=%.2fs query=%r",
        len(dois) if dois is not None else None,
        time.monotonic() - started_at,
        the_query,
    )

    #emit("dois", {"dois": dois})
