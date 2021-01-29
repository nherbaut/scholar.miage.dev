from flask import Flask, render_template, request, session, redirect
import requests
import itertools
import os
import urllib
from flask_socketio import SocketIO, emit, send
from mendeley import Mendeley
from mendeley.session import MendeleySession


API_KEY = os.environ["API_KEY"]
REDIRECT_URI = os.environ["REDIRECT_URI"]
clientId = os.environ["MENDELEY_CLIENT_ID"]
clientSecret = os.environ["MENDELEY_CLIENT_SECRET"]
mendeley = Mendeley(clientId, clientSecret, REDIRECT_URI)
SCPUS_BACKEND = f'https://api.elsevier.com/content/search/scopus?start=%d&count=%d&query=%s&apiKey={API_KEY}'

app = Flask(__name__)
app.secret_key = clientSecret
socketio = SocketIO(app)
socketio.init_app(app, cors_allowed_origins="*")


@app.route("/robots.txt")
def block_robots():
    return """User-agent: *    
Disallow: /"""


@app.route('/')
@app.route('/home')
def home():
    if 'token' in session:
        return render_template('index.html',token=session['token'])
    else:
        auth = mendeley.start_authorization_code_flow()
        session['state'] = auth.state
        return render_template('index.html',login_url=auth.get_login_url())


@app.route("/mendeleyLogout")
def mendeleyLogout():
    session.clear()
    return redirect('/')

@app.route('/oauth')
def auth_return():
    auth = mendeley.start_authorization_code_flow(state=session['state'])
    mendeley_session = auth.authenticate(request.url)

    session.clear()
    session['token'] = mendeley_session.token

    return redirect('/')

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

def get_session_from_cookies():
    return MendeleySession(mendeley, session['token'])

if __name__ == '__main__':
    app.config['SECRET_KEY'] = 'secret!'
    print(f"Using API_KEY={API_KEY}")
    socketio.run(app, host="0.0.0.0")
