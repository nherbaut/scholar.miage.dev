from feedgen.feed import FeedGenerator
import requests
import datetime
import re
import dateparser
import pytz
import urllib.parse

from app.main import SCPUS_BACKEND, API_KEY, ROOT_URL, SHLINK_API_KEY


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
                                         "title": ("°" if item["X-OA"] else "") + item["title"],
                                         "pubdate": item["x-precise-date"],
                                         "author": {"email": item["pubtitle"], "name": item["X-authors"]},
                                         "x-added-on": datetime.datetime.utcnow(),
                                         "description": f"{item['X-abstract']} \n written by {item['X-authors']}  Published by {item['pubtitle']} try to access it on <a href='{'https://sci-hub.se/' + doi}'>scihub here</a>"}


def get_results_for_query(count, query, xref, emitt=lambda *args, **kwargs: None):
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

            doi = aa.get("prism:doi", "")
            if doi != "" and xref:
                data = requests.get(f"https://api.crossref.org/works/{doi}").json()["message"]
                first_author = [a for a in data.get("author", []) if a.get("sequence", "") == "first"]
                authors = " and ".join([a.get("family","")+", " +a.get("given","") for a in data.get("author",[])])
                if len(first_author) == 0:
                    first_author_orcid = ""
                    first_author = "?"
                else:
                    first_author_orcid = first_author[0].get("ORCID", "").split("/")[-1]
                    first_author = f"{first_author[0]['family']}, {first_author[0]['given'][0]}"
                    precise_date=pytz.timezone("UTC").localize(dateparser.parse("-".join([str(date) for date in data["created"]["date-parts"][0]])))
                bucket.append({"doi": data["DOI"], "title": data["title"][0], "year": data["created"]["date-parts"][0][0],
                               "x-precise-date": str(precise_date),
                               "pubtitle": aa.get('prism:publicationName', ""),

                               "X-OA": aa.get('openaccessFlag', False),
                               "X-FirstAuthor": first_author,
                               "X-FirstAuthor-ORCID": first_author_orcid,
                               "X-IsReferencedByCount": data.get("is-referenced-by-count", -1),
                               "X-subject": ", ".join(data.get("subject", [])),
                               "X-refcount": data.get("reference-count", ""),
                               "X-abstract": data.get("abstract", ""),
                               "X-authors" : authors
                               })

            else:

                coverDate = aa.get("prism:coverDate", "")
                if coverDate == "":
                    coverDate = datetime.datetime.utcnow()
                else:
                    coverDate = dateparser.parse(coverDate)

                coverDate = pytz.timezone("UTC").localize(coverDate)

                bucket.append({"doi": aa.get("prism:doi", ""), "title": aa.get("dc:title", "-"), "year": year,
                               "x-precise-date": str(coverDate),
                               "pubtitle": aa.get('prism:publicationName', ""),

                               "X-OA": aa.get('openaccessFlag', False),
                               "X-FirstAuthor": aa.get('dc:creator', "unknown"),
                               "X-authors": aa.get('dc:creator', "unknown"),
                               });
            emitt('doi_update', {"total": count, "done": success, "failed": failed})
            emitt('doi_results', [bucket[-1]])
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


def create_short_link(query):
    long_url = f"{ROOT_URL}/permalink?query={escape_query(query)}"
    if SHLINK_API_KEY is None:
        return long_url
    body = {
        "longUrl": long_url,
        "tags": [
            "miage scholar"
        ],
        "findIfExists": True,
        "domain": "s.miage.dev",
        "shortCodeLength": 5,
        "validateUrl": True,
        "title": f"Miage scholar permalink for {query}",
        "crawlable": False
    }
    headers = {'X-Api-Key': SHLINK_API_KEY, "Content-type": "application/json"}
    resp = requests.post("https://s.miage.dev/rest/v2/short-urls", json=body, headers=headers)
    if resp.status_code == 200:
        result_url = resp.json().get("shortUrl")
    else:
        result_url = long_url
    return result_url
