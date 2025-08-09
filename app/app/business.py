from feedgen.feed import FeedGenerator
from Levenshtein.StringMatcher import distance
import requests
import datetime
import re
import dateparser
import pytz
import urllib.parse
import csv
import os
from sqlalchemy import or_
from requests_cache import CachedSession
from datetime import timedelta
from app.model import PublicationSource, Ranking
from app.main import SCPUS_BACKEND, SCPUS_ABTRACT_BACKEND, API_KEY, ROOT_URL, SHLINK_API_KEY, REDIS_URL, db
import pycountry
import logging

logger = logging.getLogger('business')

MAX_RESULTS_QUERY = 1000


def get_sources():
    sources = db.session.query(PublicationSource).all()
    res = {}
    for entry in sources:
        if entry.category in res:
            res[entry.category].append(entry)
        else:
            res[entry.category] = [entry]
    return res


def setup_redis_cache(redis_host, redis_port):
    logger.info(f"setting up redis {redis_host} {redis_port}")
    session_xref = CachedSession(
        'xrefCache',
        backend='redis',
        host=redis_host,
        port=redis_port,
        expire_after=timedelta(days=365),
        allowable_methods=['GET'],
        stale_if_error=True,
    )
    session_scpus = CachedSession(
        'scpusCache',
        host=redis_host,
        port=redis_port,
        backend='redis',
        use_cache_dir=True,
        expire_after=timedelta(days=1),
        stale_if_error=True,
    )

    return session_xref, session_scpus


def setup_fs_cache():
    session_xref = CachedSession(
        'xrefCache',
        backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=365),
        allowable_methods=['GET'],
        stale_if_error=True,
    )
    session_scpus = CachedSession(
        'scpusCache',
        backend='filesystem',
        use_cache_dir=True,
        expire_after=timedelta(days=1),
        stale_if_error=True,
    )

    return session_xref, session_scpus


try:
    if REDIS_URL != "":
        redis_host, redis_port = REDIS_URL.split(":")
        session_xref, session_scpus = setup_redis_cache(redis_host, redis_port)
        logger.info("using redis cache")
    else:
        session_xref, session_scpus = setup_fs_cache()
        logger.info("using rs cache")
except:
    session_xref, session_scpus = setup_fs_cache()
    logger.info("using rs cache")


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
                                         "title": (" " if item["X-OA"] else "") + item["title"],
                                         "pubdate": item["x-precise-date"],
                                         "author": {"email": item["pubtitle"], "name": item["X-authors"]},
                                         "x-added-on": datetime.datetime.utcnow(),
                                         "description": f"{item.get('X-abstract', '')} \n written by {item['X-authors']}  Published by {item['pubtitle']} try to access it on <a href='{'https://sci-hub.se/' + doi}'>scihub here</a>"}


def get_blank_ranking():
    return {"title": "", "acronym": "", "source": "", "rank": "", "hindex": ""}


def rank_dto_converter(rank_entity):
    res = {}
    if rank_entity.title is not None:
        res.update({"title": rank_entity.title})
    if rank_entity.acr is not None:
        res.update({"acronym": rank_entity.acr})
    if rank_entity.source is not None:
        res.update({"source": rank_entity.source})
    if rank_entity.rank is not None and rank_entity.rank != "-":
        res.update({"rank": rank_entity.rank})
    if rank_entity.hindex is not None:
        res.update({"hindex": rank_entity.hindex})
    return res


def get_ranking(conf_or_journal):
    conf_or_journal_lower = conf_or_journal.lower()
    conf_or_journal_lower = conf_or_journal_lower.replace("&amp;", "and")
    conf_or_journal_lower = conf_or_journal_lower.replace("&", "and")

    # try to find by acronym

    rank_dto_acronym = get_ranking_by_acronym(conf_or_journal)

    conf_or_journal_lower = conf_or_journal_lower.lower()
    ranks = db.session.query(Ranking)
    for word in conf_or_journal_lower.split(" "):
        ranks = ranks.filter(Ranking.title.contains(word))

    rank_dto_title = get_blank_ranking()
    ranks = ranks.order_by(Ranking.source.desc()).all()
    for rank in ranks:
        rank_title=rank.title.lower().replace("proceedings of","")
        if rank_title in conf_or_journal_lower or conf_or_journal_lower in rank_title or distance(conf_or_journal_lower, rank_title) < 5:
            rank_dto_title = rank_dto_converter(rank)
            break

    rank_dto_title.update(rank_dto_acronym)
    return rank_dto_title


def get_ranking_by_acronym(conf_or_journal):
    acrs = set()
    acrs.update(re.findall("\(([A-Za-z]+)\)", conf_or_journal))
    # acrs.update(re.findall("([A-Za-z]{3,})(?:\s|$)", conf_or_journal))
    if len(acrs) > 0:
        ranks = db.session.query(Ranking).filter(or_(Ranking.acr == v for v in acrs)).all()
        if len(ranks) > 0:
            return rank_dto_converter(ranks[0])
    return {}


def refresh_ranking():
    for rank in db.session.query(Ranking).all():
        db.session.delete(rank)
    db.session.commit()
    base_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranking")
    with open(os.path.join(base_folder, 'CORE2021.csv'), newline='\n') as csvfile:
        core_conf_reader = csv.reader(csvfile, delimiter=',')
        for row in core_conf_reader:
            if row[0].startswith("#"):
                continue
            ranking = Ranking(id=int(row[0]),
                              type="c",
                              title=row[1].lower(),
                              acr=row[2],
                              source=row[3],
                              rank=row[4])
            db.session.add(ranking)
        db.session.commit()
    with open(os.path.join(base_folder, 'CORE2018.csv'), newline='\n') as csvfile:
        core_conf_reader = csv.reader(csvfile, delimiter=',')
        for row in core_conf_reader:
            if row[0].startswith("#"):
                continue
            ranking = Ranking(id=int(row[0]),
                              type="c",
                              title=row[1].lower(),
                              acr=row[2],
                              source=row[3],
                              rank=row[4])
            db.session.add(ranking)
        db.session.commit()
    with open(os.path.join(base_folder, 'scimagojr2020.csv'), newline='\n') as csvfile:
        reader = csv.DictReader(csvfile, delimiter=';')

        for row in reader:
            ranking = Ranking(id=row["Sourceid"],
                              type="j",
                              title=row["Title"].lower(),
                              acr="",
                              source="scimagojr2020",
                              rank=row["SJR Best Quartile"],
                              hindex=row["H index"])
            db.session.add(ranking)
        db.session.commit()


def get_ref_for_doi(doi):
    resp = requests.get(SCPUS_ABTRACT_BACKEND % doi, headers={"Accept": "application/json"})
    result = resp.json()
    print(result)


def get_results_for_query(count, query, xref, emitt=lambda *args, **kwargs: None):
    dois = []

    failed = 0
    success = 0
    client_results_bucket_size = min(max(10, count / 20), 200)
    client_bucket = []
    for i in range(0, min(MAX_RESULTS_QUERY, count), 25):
        bucket = []
        print(f"SCPUS_BACKEND {SCPUS_BACKEND}")
        partial_results = session_scpus.get(
            SCPUS_BACKEND % (i, 25, escape_query(query))).json()
        for entry in partial_results["search-results"]["entry"]:
            if "prism:doi" in entry:
                success += 1
            else:
                failed += 1

            doi = entry.get("prism:doi", "")
            if doi != "" and xref:
                xref_response = session_xref.get(f"https://api.crossref.org/works/{doi}")
                if xref_response.status_code == 200:
                    xref_json_resp = xref_response.json()["message"]
                    try:
                        load_response_from_xref(bucket, xref_json_resp, entry)
                    except:
                        load_response_from_scpus(bucket, entry)
                else:
                    load_response_from_scpus(bucket, entry)
            else:
                load_response_from_scpus(bucket, entry)
        emitt('doi_update', {"total": count, "done": success, "failed": failed})
        client_bucket += bucket
        if len(client_bucket) > client_results_bucket_size:
            emitt('doi_results', client_bucket)
            client_bucket = []
        dois = dois + bucket
    emitt('doi_results', client_bucket)
    emitt('doi_export_done', dois)
    return dois


def load_response_from_scpus(bucket, entry):
    year = entry.get('prism:coverDisplayDate', "")
    if year != "":
        rematch = re.findall("[0-9]{4}", year)
        if len(rematch) > 0:
            year = rematch[0]
    coverDate = entry.get("prism:coverDate", "")
    if coverDate == "":
        coverDate = datetime.datetime.utcnow()
    else:
        coverDate = dateparser.parse(coverDate)
    coverDate = pytz.timezone("UTC").localize(coverDate)

    first_author_country = get_first_auth_country(entry)
    first_affiliation = get_first_auth_affil(entry)
    issn = entry.get("prism:issn", None)
    if not issn:
        issn = entry.get("prism:eIssn", "")

    bucket.append(
        {"doi": entry.get("prism:doi", ""), "issn": issn, "title": entry.get("dc:title", "-"),
         "year": year,
         "x-precise-date": str(coverDate),
         "pubtitle": entry.get('prism:publicationName', ""),
         "scopis_id": entry.get('dc:identifier', ""),
         "X-OA": entry.get('openaccessFlag', False),
         "X-FirstAuthor": entry.get('dc:creator', "unknown"),
         "X-Country-First-Author": first_author_country,
         "X-Country-First-affiliation": first_affiliation,
         "X-FirstAuthor-ORCID": "",
         "X-authors": entry.get('dc:creator', "unknown"),
         });


def get_first_auth_affil(entry):
    res = entry.get("affiliation", [{}])[0].get("affilname", "")
    if res is not None:
        return res
    else:
        return ""


def get_first_auth_country(entry):
    country = entry.get("affiliation", [{}])[0].get("affiliation-country", None)
    if country:
        try:
            fuzzy_country_list = pycountry.countries.search_fuzzy(country)
            if len(fuzzy_country_list) > 0:
                return fuzzy_country_list[0].alpha_3.lower()
        except:
            return "xxx"

    return "xxx"


def load_response_from_xref(bucket, xref_json_resp, entry):
    first_author = [a for a in xref_json_resp.get("author", []) if a.get("sequence", "") == "first"]
    authors = " and ".join([a.get("family", "") + ", " + a.get("given", "") for a in xref_json_resp.get("author", [])])

    first_author_country = get_first_auth_country(entry)
    first_affiliation = get_first_auth_affil(entry)

    if len(first_author) == 0:
        first_author_orcid = ""
        first_author = "?"
    else:
        first_author_orcid = first_author[0].get("ORCID", "").split("/")[-1]
        first_author = f"{first_author[0]['family']}, {first_author[0]['given'][0]}"
    precise_date = pytz.timezone("UTC").localize(
        dateparser.parse("-".join([str(date) for date in xref_json_resp["created"]["date-parts"][0]])))

    ranking_info = get_ranking(xref_json_resp["container-title"][0])
    if ranking_info is not None:
        pub_rank = ranking_info["rank"]
        rank_source = f"{ranking_info['source']}"
        hindex = f"{ranking_info['hindex']}"
    else:
        pub_rank = "?"
        rank_source = ""
        hindex = ""

    bucket.append({"doi": xref_json_resp["DOI"], "title": xref_json_resp["title"][0],
                   "year": xref_json_resp["created"]["date-parts"][0][0],
                   "x-precise-date": str(precise_date),
                   "pubtitle": xref_json_resp["container-title"][0],
                   "pub_rank": pub_rank,
                   "rank_source": rank_source,
                   "hindex": hindex,
                   "X-OA": entry.get('openaccessFlag', False),
                   "X-FirstAuthor": first_author,
                   "X-Country-First-Author": first_author_country,
                   "X-Country-First-affiliation": first_affiliation,
                   "X-FirstAuthor-ORCID": first_author_orcid,
                   "X-IsReferencedByCount": xref_json_resp.get("is-referenced-by-count", -1),
                   "X-subject": ", ".join(xref_json_resp.get("subject", [])),
                   "X-refcount": xref_json_resp.get("reference-count", ""),
                   "X-abstract": xref_json_resp.get("abstract", ""),
                   "X-authors": authors
                   })


def escape_query(query):
    return urllib.parse.quote(query)


def count_results_for_query(query):
    print(f"query with {API_KEY} API_KEY")
    response = session_scpus.get(SCPUS_BACKEND % (0, 1, escape_query(query))).json()

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
        "domain": "s.miage.nextnet.top",
        "shortCodeLength": 5,
        "validateUrl": True,
        "title": f"Miage scholar permalink for {query[:30]}",
        "crawlable": False
    }
    headers = {'X-Api-Key': SHLINK_API_KEY, "Content-type": "application/json"}
    resp = requests.post("https://s.miage.nextnet.top/rest/v2/short-urls", json=body, headers=headers)
    if resp.status_code == 200:
        result_url = resp.json().get("shortUrl")
    else:
        result_url = long_url
    return result_url
