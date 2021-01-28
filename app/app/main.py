from flask import Flask, render_template, request
import requests
import itertools
import os
import urllib
from flask_socketio import SocketIO, emit, send

app = Flask(__name__)
socketio = SocketIO(app)
socketio.init_app(app, cors_allowed_origins="*")
API_KEY = os.environ["API_KEY"]

SCPUS_BACKEND = f'https://api.elsevier.com/content/search/scopus?start=%d&count=%d&query=%s&apiKey={API_KEY}'


@app.route('/')
@app.route('/home')
@app.route('/query', methods=["GET"])
def hello_world():
    return render_template('index.html')


@app.route('/query', methods=["POST"])
def query():
    the_query = request.form.get("query")

    count, is_count, p = count_results_for_query(the_query)

    print(f"fetching {count} results")
    if is_count:
        return render_template('index.html', query=the_query, count=count)
    else:
        refs = get_results_for_query(count, the_query)
        dois = [r.get("prism:doi") for r in refs if "prism:doi" in r]
        return render_template('dois.html', dois=dois)


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
            bucket.append({"doi": aa.get("prism:doi", ""), "title": aa.get("dc:title", "???")});
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
    emit("count", count_results_for_query(json_data["query"]))


@socketio.on('get_dois')
def handle_get_dois(json_data):
    the_query = json_data["query"]
    count = count_results_for_query(the_query)
    dois = get_results_for_query(count, the_query)

    emit("dois", {"dois": dois})


if __name__ == '__main__':
    app.config['SECRET_KEY'] = 'secret!'
    print(f"Using API_KEY={API_KEY}")
    socketio.run(app,host="0.0.0.0")
