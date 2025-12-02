import requests

from app.main import app, db
from app.model import ScpusFeed, ScpusRequest, PublicationSource, NetworkData
from app.business import count_results_for_query, get_papers, update_feed, generate_rss, get_sources, \
    get_ref_for_doi, get_ranking, refresh_ranking, net_get_graph_data
from app.query_analyzer import get_json_analyzed_query
from flask import abort, Response, render_template, request, session, redirect, url_for, send_from_directory
# from mendeley import Mendeley
# from mendeley.session import MendeleySession
# from mendeley.exception import MendeleyException, MendeleyApiException
import json
import pickle
import os
from app.researchers import get_venue_for_orcid, get_venue_for_openalex
from collections import Counter

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
        conf = db.session.query(PublicationSource).filter(
            PublicationSource.short_name == short_name).one()
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
    return render_template('feeds.html', feeds=feeds, active_page="feeds")


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

    count_scopus, count_arxiv = count_results_for_query(
        feed.query, include_arxiv=True)
    if count_scopus+count_arxiv != feed.count:
        dois = get_papers(count_scopus, feed.query, arxiv=True, xref=True,
                          existing_data=pickle.loads(feed.feed_content), count_arxiv=count_arxiv)
    else:
        dois = []
    if feed.feed_content is not None:
        feed_content = pickle.loads(feed.feed_content)
    else:
        feed_content = {}

    update_feed(dois, feed_content)
    feed.count = count_scopus+count_arxiv
    feed.feed_content = pickle.dumps(feed_content)
    feed.hit += 1
    db.session.commit()

    feed_items = sorted(feed_content.values(),
                        key=lambda x: x["pubdate"], reverse=True)

    rss = generate_rss(feed_items, feed.id, feed.query)

    return Response(rss, mimetype='application/atom+xml')


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


@app.route('/stars', methods=["GET"])
def stars_page():
    # Client-side page; data loaded from localStorage in the browser
    return render_template('stars.html', sources=get_sources(), active_page="stars")


@app.route("/doi/<doi1>/<doi2>", methods=["GET"])
def get_info_for_doi(doi1, doi2):
    get_ref_for_doi(doi1 + "/" + doi2)


@app.route("/doi", methods=["GET"])
def get_doi_for_title():
    title = request.args.get('title')
    query = f"TITLE({title})"
    count_scopus, _ = count_results_for_query(query)
    dois = get_papers(count_scopus, query, False)
    if (len(dois) == 0):
        abort(404)
    return app.response_class(
        response=json.dumps(dois[0]["doi"]),
        status=200,
        mimetype='application/json'
    )


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
            response=json.dumps(
                {"doi": doi, "citation": resp.content.decode("utf-8")}),
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
    queries = db.session.query(ScpusRequest).order_by(
        ScpusRequest.timestamp.desc()).limit(limit).all()
    accepts = request.headers["Accept"].split(",")
    if "application/json" in accepts:
        return app.response_class(
            response=json.dumps([q.query for q in queries]),
            status=200,
            mimetype='application/json'
        )

    else:
        return render_template('history.html', queries=queries, active_page="history")


@app.route("/refresh_ranking", methods=["GET"])
def refresh_ranking_rest():
    refresh_ranking()
    return "UPDATED", 204


@app.route("/rank", methods=["GET"])
def getRanking():
    query = request.args.get("query")
    type = request.args.get("type")
    res = get_ranking(query)

    if type == "img":

        if res is None:
            return redirect("static/img/qm.png", code=302)
        if res["rank"] == "A*":
            return redirect("static/img/as.png", code=302)
        if res["rank"] == "A":
            return redirect("static/img/a.png", code=302)
        if res["rank"] == "B":
            return redirect("static/img/b.png", code=302)
        if res["rank"] == "C":
            return redirect("static/img/c.png", code=302)
        return redirect("static/img/qm.png", code=302)

    if type == "txt":
        if res is None:
            return "?"
        else:
            return res["rank"]

    else:
        if res is not None:
            return app.response_class(
                response=json.dumps(res),
                status=200,
                mimetype='application/json'
            )
        else:
            return abort(404, description="No Ranking Available")


@app.route('/snowball', methods=["GET"])
def snowball():
    accepts = request.headers["Accept"].split(",")
    if "application/json" in accepts:
        title = request.args.get('title')
        query = f"REFTITLE(\"{title}\")"
        count = count_results_for_query(query)
        dois = get_papers(count, query, False)
        return app.response_class(
            response=json.dumps([doi["doi"] for doi in dois]),
            status=200,
            mimetype='application/json'
        )

    else:
        title = request.args.get('title')
        return render_template('index.html', query=f"REFTITLE(\"{title}\")", sources=get_sources())


@app.route("/dois-list", methods=["GET"])
def doi_list():
    dois = request.args.get('dois').split(",")

    return render_template('index.html', query=" OR ".join([f'DOI("{doi}")' for doi in dois]), sources=get_sources())


@app.route('/sameauthor', methods=["GET"])
def same_author():
    name = request.args.get('name')
    orcid = request.args.get('orcid')
    if orcid:
        return render_template('index.html', query=f"ORCID({orcid})", sources=get_sources())
    else:
        return render_template('index.html', query=f"AUTHOR-NAME({name})", sources=get_sources())


@app.route('/sameauthor-and-conf', methods=["GET"])
def same_author_and_conf():
    source = request.args.get('source')
    orcid = request.args.get('orcid')
    return render_template('index.html', query=f"ORCID({orcid}) AND EXACTSRCTITLE({source})", sources=get_sources())


@app.route("/venues", methods=["GET"])
def get_venues_form():
    # Accept multiple identifiers via repeated params or comma/space-separated lists
    def _collect_list(param_name: str):
        vals = request.args.getlist(param_name)
        out = []
        for v in vals:
            if not v:
                continue
            # split on commas or whitespace
            parts = [p.strip()
                     for p in re.split(r"[\s,]+", v) if p and p.strip()]
            out.extend(parts)
        return out

    # Fallback single values if present (kept for backward compat in template)
    orcid_single = request.args.get('orcid')
    openalex_single = request.args.get('openalex')

    # Full lists
    try:
        import re  # local import to avoid top-level impact
        orcids = _collect_list('orcid')
        openalexes = _collect_list('openalex')
    except Exception:
        # In case 're' or splitting fails for some reason, degrade gracefully
        orcids = [v for v in request.args.getlist('orcid') if v]
        openalexes = [v for v in request.args.getlist('openalex') if v]

    return render_template(
        'venues.html',
        orcid=orcid_single,
        openalex=openalex_single,
        orcids=orcids,
        openalexes=openalexes,
        sources=get_sources(),
        active_page="venues"
    )


@app.route('/opensearch', methods=["GET"])
def opensearch():
    query = request.args.get('query')

    return render_template('index.html', query=f"TITLE(\"{query}\")", sources=get_sources())


@app.route("/network/compute/<id>", methods=["GET"])
def get_network_data(id):
    return app.response_class(
        response=net_get_graph_data(id),
        status=200,
        mimetype='application/json'
    )


@app.route('/network/<work_list_id>', methods=["GET"])
def get_network_page(work_list_id):
    return render_template('network.html', work_list_id=work_list_id,  sources=get_sources())


@app.route('/networks', methods=["GET"])
def get_networks_page():

    try:
        networks_data = db.session.query(NetworkData).all()

        return render_template('networks.html', networks=networks_data, active_page="networks")
    except:
        return render_template('index.html',   sources=get_sources())


@app.route('/query/analysis', methods=["GET"])
def query_analysis_page():
    default_query = request.args.get("query", "")
    return render_template('query_analysis.html', query=default_query, show_run_button=True, sources=get_sources(), active_page="debug")


@app.route('/query/analysis/<query_id>', methods=["GET"])
def query_analysis_saved(query_id: int):
    try:
        saved_request = db.session.query(ScpusRequest).filter(
            ScpusRequest.id == int(query_id)).one()
        return render_template('query_analysis.html', query=saved_request.query, show_run_button=False, sources=get_sources())
    except:
        return render_template('query_analysis.html', query="", show_run_button=False, sources=get_sources(), active_page="debug")


@app.route('/query/analysis', methods=["POST"])
def analyze_query():
    payload = request.get_json() or {}
    query = payload.get("query")
    if not query:
        return abort(400, description="Missing query")
    data = get_json_analyzed_query(query, count_results_for_query)
    return app.response_class(
        response=data,
        status=200,
        mimetype='application/json'
    )


@app.route('/permalink/<query_id>', methods=["GET"])
def permalink(query_id: int):

    try:
        request = db.session.query(ScpusRequest).filter(
            ScpusRequest.id == int(query_id)).one()

        return render_template('index.html', query=request.query,  sources=get_sources())
    except:
        return render_template('index.html',   sources=get_sources())
