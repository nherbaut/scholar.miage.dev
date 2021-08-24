from feedgen.feed import FeedGenerator
import requests
import datetime
import re
import dateparser
import pytz
import urllib.parse

from app.main import SCPUS_BACKEND, API_KEY, ROOT_URL


def generate_rss(feed_items, id="id", query="query"):
    fg = FeedGenerator()
    for item in feed_items:
        fe = fg.add_entry()
        for key, value in item.items():
            if key.startswith("x-"):
                continue
            setter = getattr(fe, key)

            if isinstance(value, list):
                for value_item in value:
                    setter(value_item)
            else:
                setter(value)
    fg.title(f"Bibliography Feed {id}")
    fg.link({"href": f'{ROOT_URL}/feed/{id}.rss', "rel": 'alternate'})
    fg.description(f"results for query: {query}")
    return fg.rss_str()


def update_feed(dois, feed_content):
    for item in dois:
        if item["doi"] != "" and item["doi"] not in feed_content:
            doi = item["doi"]
            feed_content[item["doi"]] = {"content": "https://doi.org/" + doi,
                                         "link": [{"href": "https://doi.org/" + doi,
                                                   "rel": "alternate",
                                                   "title": "publisher's site"},
                                                  {"href": ROOT_URL,
                                                   "rel": "via",
                                                   "title": "Authoring search engine"},
                                                  {"href": "https://sci-hub.se/" + doi,
                                                   "rel": "related",
                                                   "title": "SciHub link"}
                                                  ],
                                         "title": ("°" if item["X-OA"] else "")+ item["title"],
                                         "pubdate": item["X-coverDate"],
                                         "author": {"email": item["pubtitle"], "name":item["X-FirstAuthor"]},
                                         "x-added-on": datetime.datetime.utcnow(),
                                         "description": f"written by {item['X-FirstAuthor']} et as. Published by {item['pubtitle']} try to access it on <a href='{'https://sci-hub.se/' + doi}'>scihub here</a>"}


def get_results_for_query(count, query, emitt=lambda *args, **kwargs: None):
    dois = []

    failed = 0
    success = 0
    for i in range(0, min(1000, count), 25):
        bucket = []
        partial_results = requests.get(
            SCPUS_BACKEND % (i, 25, escape_query(query))).json()
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
            coverDate = aa.get("prism:coverDate", "")
            if coverDate == "":
                coverDate = datetime.datetime.utcnow()
            else:
                coverDate = dateparser.parse(coverDate)

            coverDate = pytz.timezone("UTC").localize(coverDate)
            bucket.append({"doi": aa.get("prism:doi", ""), "title": aa.get("dc:title", "-"), "year": year,
                           "pubtitle": aa.get('prism:publicationName', ""),
                           "X-coverDate": str(coverDate),
                           "X-OA": aa.get('openaccessFlag', False),
                           "X-FirstAuthor": aa.get('dc:creator', "unknown")});
            emitt('doi_update', {"total": count, "done": success, "failed": failed})
        emitt('doi_results', bucket)
        dois = dois + bucket
    emitt('doi_export_done', dois)
    return dois


def escape_query(query):
    return urllib.parse.quote(query)


def count_results_for_query(query):
    print(f"query with {API_KEY} API_KEY")
    response = requests.get(SCPUS_BACKEND % (0, 1, escape_query(query))).json()
    print(response)

    if "search-results" in response:

        count = int(response["search-results"]["opensearch:totalResults"])
        return count
    else:

        return 0
