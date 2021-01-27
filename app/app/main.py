from flask import Flask, render_template, request
import requests
import itertools
import os
import urllib
app = Flask(__name__)


@app.route('/')
@app.route('/home')
@app.route('/query', methods=["GET"])
def hello_world():
    return render_template('index.html')



@app.route('/query', methods=["POST"])
def query():
    query = request.form.get("query")

    is_count = request.form.get("count")

    p = f'https://api.elsevier.com/content/search/scopus?start=%d&count=%d&query=%s&apiKey={os.environ("API_KEY")}'
    count = int(requests.get(p % (0, 1,  query.replace(" ", "+").replace("\\", "%%22"))).json()["search-results"][
                    "opensearch:totalResults"])

    print(f"fetching {count} results")
    if is_count:
        return render_template('index.html', query=query, count=count)
    else:
        refs = list(itertools.chain(*[aa["search-results"]["entry"] for aa in
                                      [requests.get(p % (i, 25, query.replace(" ", "+").replace("\\", "%%22"))).json() for i
                                       in
                                       range(0, min(1000,count), 25)]]))
        dois = [ r.get("prism:doi") for r in refs if "prism:doi" in r]
        return render_template('dois.html',dois=dois)


if __name__ == '__main__':
    app.run()
