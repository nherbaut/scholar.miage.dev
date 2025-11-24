# Standard library
import pickle
import csv
import datetime
from datetime import timedelta, timezone
import json
import logging
import os
import re
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Set, Tuple
import tempfile
import contextlib
from urllib.error import HTTPError
from concurrent.futures import as_completed

# Third-party libraries
import dateparser
import pycountry
import pytz
import requests
from Levenshtein.StringMatcher import distance
from feedgen.feed import FeedGenerator
from flask import copy_current_request_context
from requests_cache import CachedSession, FileCache, RedisCache
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm.exc import MultipleResultsFound
from sqlalchemy import or_
from urllib3.util import Retry

# pyalex (third-party, grouped separately for clarity)
import pyalex
from pyalex import Authors, Funders, Institutions, Publishers, Sources, Topics, Works, config as pyalex_config

# Local application
from app.cache import session_scpus, session_xref
from app.main import (
    API_KEY,
    ROOT_URL,
    SCPUS_ABTRACT_BACKEND,
    SCPUS_BACKEND,
    SHLINK_API_KEY,
    db,
)
from app.model import PublicationSource, Ranking, NetworkData

pyalex_config.email = os.getenv("PYALEX_EMAIL", "nico@scholar.miage.dev")
pyalex_config.max_retries = 3
pyalex_config.retry_backoff_factor = 0.2
pyalex_config.retry_http_codes = [429, 500, 503]

executor_openAlex = ThreadPoolExecutor(max_workers=9)
executor_scopus = ThreadPoolExecutor(max_workers=9)

_DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\\.org/", flags=re.I)
_OA_PREFIX_RE = re.compile(r"^https?://openalex\\.org/", flags=re.I)


def _cached_requests_session():
    REDIS_URL = os.environ.get("REDIS_URL", "")

    if REDIS_URL:
        redis_host, redis_port = REDIS_URL.split(":")
        s = CachedSession(
            'openAlexCAche',
            backend='redis',
            host=redis_host,
            port=redis_port,
            expire_after=timedelta(days=365),
            allowable_methods=['GET'],
            stale_if_error=True,
        )
    else:
        s = CachedSession(
            backend=FileCache(),
            expire_after=timedelta(days=1),
            allowable_methods=['GET'],
            stale_if_error=True,
        )

    retries = Retry(
        total=pyalex.config.max_retries,
        backoff_factor=pyalex.config.retry_backoff_factor,
        status_forcelist=pyalex.config.retry_http_codes,
        allowed_methods=frozenset({"GET"}),
    )
    s.mount("https://", requests.adapters.HTTPAdapter(max_retries=retries))
    return s


pyalex._get_requests_session = _cached_requests_session


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


def generate_rss(feed_items, id="id", query="query"):
    fg = FeedGenerator()
    for item in reversed(feed_items):
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
        fe.content(item["description"])
    fg.title(f"Bibliography Feed {id}")
    fg.link({"href": f'{ROOT_URL}/feed/{id}.rss', "rel": 'alternate'})
    fg.description(f"results for query: {query}")

    return fg.rss_str()


def update_feed(dois, feed_content):
    for item in dois:
        if item["doi"] != "" and item["doi"] not in feed_content:
            doi = item["doi"]
            access_link = ""
            if "X-OA-URL" in item and item["X-OA-URL"] and len(item["X-OA-URL"]) > 0:
                access_link = item["X-OA-URL"]
                item["X-OA"] = True
                description = f"{item.get('X-abstract', '')} \n written by {item['X-authors']}  Published by {item['pubtitle']}. \n We think we have found an OA link here:  <a href='{access_link}'>this site</a>"
            else:
                access_link = f"https://scholar.google.com/scholar?q={item['title']}"
                description = f"{item.get('X-abstract', '')} \n written by {item['X-authors']}  Published by {item['pubtitle']}\n We didn't find an OA link, try to find a OA version on <a href='{access_link}'>Google Scholar</a>"

            feed_content[item["doi"]] = {"content":  doi,
                                         "link": [{"href": doi,
                                                   "rel": "alternate",
                                                   "title": "publisher's site"},
                                                  {"href": ROOT_URL,
                                                   "rel": "via",
                                                   "title": "Authoring search engine"},
                                                  {"href": f"https://scholar.google.com/scholar?q={item['title']}",
                                                   "rel": "related",
                                                   "title": "Google Scholar link"}
                                                  ],
                                         "title": (" [PDF] " if item["X-OA"] else "") + item["title"],
                                         "pubdate": dateparser.parse(item["x-precise-date"]).replace(tzinfo=timezone.utc),
                                         "author": {"email": item["pubtitle"], "name": item["X-authors"]},
                                         "x-added-on": datetime.datetime.now(),
                                         "description": description}


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
        rank_title = rank.title.lower().replace("proceedings of", "")
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
        ranks = db.session.query(Ranking).filter(
            or_(Ranking.acr == v for v in acrs)).all()
        if len(ranks) > 0:
            return rank_dto_converter(ranks[0])
    return {}


def refresh_ranking():
    for rank in db.session.query(Ranking).all():
        db.session.delete(rank)
    db.session.commit()
    base_folder = os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "ranking")
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
    resp = requests.get(SCPUS_ABTRACT_BACKEND %
                        doi, headers={"Accept": "application/json"})
    result = resp.json()
    print(result)


def get_scopus_works_for_query(count, query, xref, emitt=lambda *args, **kwargs: None, existing_data={}):
    dois = []

    context = type('', (object,), {"success": 0, "failed": 0})()
    client_results_bucket_size = min(max(10, count / 20), 200)
    client_bucket = []
    for i in range(0, min(MAX_RESULTS_QUERY, count), 25):
        bucket = []
        print(f"SCPUS_BACKEND {SCPUS_BACKEND}")
        partial_results = session_scpus.get(
            SCPUS_BACKEND % (i, 25, escape_query(query))).json()

        entries = partial_results["search-results"]["entry"]

        entries = [entry for entry in entries if (entry.get(
            'prism:doi') and f"https://doi.org/{entry.get('prism:doi').lower()}" not in existing_data.keys()) or not entry.get('prism:doi')]

        if len(entries) == 0:
            break

        @copy_current_request_context
        def call_back(success, failure):
            emitt('doi_update', {"total": count,
                  "done": success, "failed": failure})

        if xref:
            futures = [
                executor_openAlex.submit(extract_data_openalex, bucket,
                                         entry, context, call_back)
                for entry in entries
            ]

            for f in futures:
                f.result()
        else:
            for entry in entries:
                extract_data_scopus(bucket, entry, context, call_back)

        client_bucket += bucket
        if len(client_bucket) > client_results_bucket_size:
            emitt('doi_results', client_bucket)
            client_bucket = []
        dois = dois + bucket
    emitt('doi_results', client_bucket)
    emitt('doi_export_done', dois)
    return dois


def complete_scopus_extraction(scopus_partial_data, r):

    oa_url = (r.get("open_access") or {}).get("oa_url", None)

    scopus_partial_data["doi"] = r["id"]
    scopus_partial_data["X-OA"] = r["open_access"]["is_oa"]
    scopus_partial_data["X-IsReferencedByCount"] = r["cited_by_count"]
    scopus_partial_data["X-subject"] = (r["primary_topic"] if "primary_topic" in r and r["primary_topic"]
                                        and len(r["primary_topic"]) else {}).get("display_name", "")
    scopus_partial_data["X-refcount"] = r["referenced_works_count"]
    scopus_partial_data["X-authors"] = ", ".join(
        [a["author"]["display_name"] for a in r["authorships"]])
    scopus_partial_data["X-authors-list"] = [{"display_name": a["author"]["display_name"], "orcid": a["author"]
                                              ["orcid"] if a["author"]["orcid"] else "", "openalex": a["author"]["id"]} for a in r["authorships"]]
    scopus_partial_data["X-OA-URL"] = oa_url


def extract_data_openalex(bucket, entry, context, call_back):
    if "prism:doi" in entry:
        context.success += 1
    else:
        context.failed += 1

    doi = entry.get("prism:doi", "")

    if len(doi) > 0:
        try:
            oa_response = Works()[f"https://doi.org/{doi}"]
            load_response_from_openAlex(bucket, oa_response, entry)
        except Exception as e:
            print(e)
            load_response_from_scpus(bucket, entry)
    else:
        try:
            oa_responses = Works().filter(
                title={"search": entry.get('prism:publicationName', "")}).get()
            if len(oa_responses) > 1:
                load_response_from_scpus(bucket, entry)
                complete_scopus_extraction(bucket[-1], oa_responses[1])
        except Exception as e:
            # something work with the openalex query, not much we can do now
            pass

    try:
        call_back(context.success, context.failed)
    except:
        pass


def extract_data_scopus(bucket, entry, context, call_back):
    if "prism:doi" in entry:
        context.success += 1
    else:
        context.failed += 1

    load_response_from_scpus(bucket, entry)

    try:
        call_back(context.success, context.failed)
    except:
        pass


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

    authors_list = [{"display_name": entry.get('dc:creator', "unknown")}]
    bucket.append(
        {"doi": "https://doi.org/"+entry.get("prism:doi", ""), "issn": issn, "title": entry.get("dc:title", "-"),
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
         "X-authors-list": authors_list
         })


def get_first_auth_affil(entry):
    res = entry.get("affiliation", [{}])[0].get("affilname", "")
    if res is not None:
        return res
    else:
        return ""


def get_first_auth_country(entry):
    country = entry.get("affiliation", [{}])[
        0].get("affiliation-country", None)
    if country:
        try:
            fuzzy_country_list = pycountry.countries.search_fuzzy(country)
            if len(fuzzy_country_list) > 0:
                return fuzzy_country_list[0].alpha_3.lower()
        except:
            return "xxx"

    return "xxx"


def inverted_abstrct_to_abstract(ia):
    if not ia:
        return ""
    iaa = {}
    for k, vv in ia.items():
        for v in vv:
            iaa[v] = k
    return " ".join([iaa[k] for k in sorted(iaa.keys())])


def get_abstract_semanticscholar(doi: str):
    """
    Retrieve paper abstract from Semantic Scholar Graph API.
    Returns None if not found or no abstract.
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    params = {"fields": "title,abstract"}
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    data = r.json()
    return data.get("abstract", "") or ""


def _download_pdf_to_temp(url: str) -> str | None:
    """Download a PDF to a temporary file and return its filepath, or None on failure."""
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            return None
        content_type = r.headers.get("Content-Type", "").lower()
        is_pdf = r.content.startswith(
            b"%PDF") or "application/pdf" in content_type
        if not is_pdf:
            return None
        fd, path = tempfile.mkstemp(prefix="oa_pdf_", suffix=".pdf")
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
        return path
    except Exception:
        return None


def _extract_abstract_from_pdf_file(pdf_path: str) -> str:
    """Use a local GROBID service to extract abstract from a PDF file path."""
    try:
        with open(pdf_path, "rb") as f:
            files = {"input": (os.path.basename(
                pdf_path), f, "application/pdf")}
            grobid = requests.post(
                "http://localhost:8070/api/processHeaderDocument", files=files, timeout=45,
                headers={"Accept": "application/xml"}
            )
        if grobid.status_code != 200:
            return ""
        import xml.etree.ElementTree as ET
        root = ET.fromstring(grobid.text)
        abs_nodes = root.findall(".//{*}abstract")
        return " ".join(" ".join(n.itertext()).strip() for n in abs_nodes).strip()
    except Exception:
        return ""


def _unpaywall_pdf_url(doi: str, email: str) -> str | None:
    """Return Unpaywall best PDF URL for a DOI, or None."""
    try:
        url = f"https://api.unpaywall.org/v2/{doi}"
        params = {"email": email}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        oa = data.get("best_oa_location") or {}
        return oa.get("url_for_pdf") or None
    except Exception:
        return None


def get_abstract_unpaywall(doi: str, email: str) -> str | None:
    """
    Retrieve abstract via Unpaywall (PDF) using GROBID. Returns empty string on failure.
    The PDF download is delegated to a helper and the file is cleaned up.
    """
    pdf_url = _unpaywall_pdf_url(doi, email)
    if not pdf_url:
        return None
    pdf_path = _download_pdf_to_temp(pdf_url)
    if not pdf_path:
        return ""
    try:
        return _extract_abstract_from_pdf_file(pdf_path)
    finally:
        with contextlib.suppress(Exception):
            os.remove(pdf_path)


def get_abstract_from_pdf_sources(doi: str, openalex_oa_url: str | None, email: str) -> str:
    """
    Try to extract abstract from a PDF by first downloading via OpenAlex OA URL,
    then falling back to Unpaywall's PDF URL. Ensures temporary files are removed.
    Returns empty string if extraction fails.
    """
    # 1) Try OpenAlex OA URL first
    if openalex_oa_url:
        pdf_path = _download_pdf_to_temp(openalex_oa_url)
        if pdf_path:
            try:
                abstract = _extract_abstract_from_pdf_file(pdf_path)
                if abstract:
                    return abstract
            finally:
                with contextlib.suppress(Exception):
                    os.remove(pdf_path)

    # 2) Fallback: Unpaywall best PDF
    pdf_url = _unpaywall_pdf_url(doi, email)
    if pdf_url:
        pdf_path = _download_pdf_to_temp(pdf_url)
        if pdf_path:
            try:
                abstract = _extract_abstract_from_pdf_file(pdf_path)
                if abstract:
                    return abstract
            finally:
                with contextlib.suppress(Exception):
                    os.remove(pdf_path)

    return ""


def load_response_from_openAlex(bucket, r, entry):

    oa_url = (r.get("open_access") or {}).get("oa_url", None)
    authors_list = [{"display_name": a["author"]["display_name"], "orcid": a["author"]["orcid"]
                     if a["author"]["orcid"] else "", "openalex": a["author"]["id"]} for a in r["authorships"]]

    abstract = inverted_abstrct_to_abstract(
        r["abstract_inverted_index"]) if "abstract_inverted_index" in r else None
    if not abstract:
        abstract = get_abstract_semanticscholar(r["doi"])
        
    # it's too slow        
    # if not abstract:
    #     abstract = get_abstract_from_pdf_sources(
    #         r["doi"], oa_url, "nicolas.herbaut@u-bordeaux.fr")
    if not abstract:
        abstract = ""

    if len(authors_list) == 0:
        authors_list = [{"display_name": entry.get(
            'dc:creator', "unknown"), "orcid": "", "openalex": ""}]

    bucket.append({"doi": r["doi"], "title": r["title"],
                   "year": r["publication_year"],
                   "x-precise-date": r["publication_date"],
                   "pubtitle": entry.get('prism:publicationName', ""),
                   "pub_rank": "",
                   "rank_source": "",
                   "hindex": "",
                   "X-OA": r["open_access"]["is_oa"],
                   "X-FirstAuthor": authors_list[0]["display_name"] if len(authors_list) > 0 else "",
                   "X-Country-First-Author": get_first_auth_country(entry),
                   "X-Country-First-affiliation": get_first_auth_affil(entry),
                   "X-FirstAuthor-ORCID": "",
                   "X-FirstAuthor-OpenAlex": "",
                   "X-IsReferencedByCount": r["cited_by_count"],
                   "X-subject": (r["primary_topic"] if "primary_topic" in r and r["primary_topic"] and len(r["primary_topic"]) else {}).get("display_name", ""),
                   "X-refcount": r["referenced_works_count"],
                   "X-abstract": abstract,
                   "X-authors": ", ".join([a["author"]["display_name"] for a in r["authorships"]]),
                   "X-authors-list": authors_list,
                   "X-OA-URL": oa_url or ""

                   })


def load_response_from_xref(bucket, xref_json_resp, entry):
    first_author = [a for a in xref_json_resp.get(
        "author", []) if a.get("sequence", "") == "first"]
    authors = " and ".join([a.get("family", "") + ", " + a.get("given", "")
                           for a in xref_json_resp.get("author", [])])
    authors_list = [{"display_name": a.get("family", "")+", " + a.get("given", "")[0], "orcid": a.get(
        "ORCID")} for a in xref_json_resp.get("author", [])]

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
                   "X-authors": authors,
                   "X-authors-list": authors_list
                   })


def escape_query(query):
    return urllib.parse.quote(query)


def count_results_for_query(query):
    #print(f"query with {API_KEY} API_KEY")
    response = session_scpus.get(SCPUS_BACKEND %
                                 (0, 1, escape_query(query))).json()

    if "search-results" in response:

        count = int(response["search-results"]["opensearch:totalResults"])
        return count
    else:

        return 0


# NETWORK (BETA)


# -------------------------------------
# Helpers
# -------------------------------------
def net_normalize_input(s: str) -> str:
    s = s.strip()
    if _DOI_PREFIX_RE.match(s):
        return s
    elif _OA_PREFIX_RE.match(s):
        return s.rsplit("/", 1)[-1]  # return bare W-id
    elif s.upper().startswith("W"):
        return s  # bare OpenAlex ID
    else:
        return f"https://doi.org/{s}"


def net_fetch_work(identifier: str) -> dict | None:
    """Fetch a single work by DOI URL or OpenAlex ID."""
    try:
        return Works()[identifier]
    except Exception:
        return None


def net_work_metadata(w: dict) -> Tuple[str, List[str], str, str, str]:
    """Extract title, authors, venue, doi_url, openalex_url."""
    if not w:
        return "", [], "", "", ""
    title = w.get("title") or ""
    authors = []
    for a in (w.get("authorships") or []):
        ao = a.get("author") if isinstance(a, dict) else None
        nm = ""
        if isinstance(ao, dict):
            nm = ao.get("display_name") or ""
        if not nm:
            nm = a.get("raw_author_name") or ""
        if nm:
            authors.append(nm)
    venue = ""
    hv = w.get("host_venue") or {}
    if isinstance(hv, dict):
        venue = hv.get("display_name") or ""
    if not venue:
        pl = w.get("primary_location") or {}
        if isinstance(pl, dict):
            src = pl.get("source") or {}
            if isinstance(src, dict):
                venue = src.get("display_name") or ""
    doi_url = None
    ids = w.get("ids") or {}
    if isinstance(ids, dict):
        doi_url = ids.get("doi")
    openalex_url = w.get("id") or ""
    return title, authors, venue, doi_url, openalex_url


def net_referenced_ids(w: dict) -> List[str]:
    """Return referenced work IDs (bare W-ids)."""
    out = []
    for r in (w.get("referenced_works") or []):
        if isinstance(r, str) and "/" in r:
            out.append(r.rsplit("/", 1)[-1])
    return out


def net_extract_keywords(w: dict) -> List[str]:
    """Return keyword strings from a work object."""
    kws = []
    for kw in (w.get("keywords") or []):
        if isinstance(kw, dict):
            val = kw.get("keyword")
        else:
            val = kw
        if val:
            val = str(val).strip()
            if val:
                kws.append(val)
    return kws


def net_get_graph_data(id):
    try:
        network_data = db.session.query(
            NetworkData).where(NetworkData.id == id).one()
        return pickle.loads(network_data.network_data)
    except MultipleResultsFound as e:
        return None, "Too many results"
    except NoResultFound as e:
        return None, "Not Found"


# -------------------------------------
# Main function
# -------------------------------------


def net_build_graph(
    dois_or_ids: Iterable[str],
    min_count: int = 2,
    emitt=lambda *args, **kwargs: None,
    executor: ThreadPoolExecutor = executor_openAlex,
    cites_limit_per_work: int = 200,   # cap for backward refs per input work
) -> dict:
    """
    Parallel implementation with forward and backward references.

    - Forward references: OpenAlex 'referenced_works' of each input work.
      Retain refs cited by >= min_count input works. Nodes: type="ref".
    - Backward references (NEW): works that cite an input work (OpenAlex 'cites' query).
      Retain citing works that cite >= min_count input works. Nodes: type="ref_back".
      NOTE: a work may appear as both a 'work' and as a 'ref'/'ref_back' (duplicated on purpose).

    The 'links' array uses:
        {"source": <work_node_id>, "target": <ref_id>, "kind": "forward" | "back"}

    IMPORTANT FIX:
    Link sources now reuse the exact node id assigned to each input work,
    so frontend hover adjacency works reliably.
    """
    # -------------------------
    # Phase 1: fetch input works in parallel
    # -------------------------
    normalized_idents: List[str] = [
        net_normalize_input(raw) for raw in dois_or_ids]
    total_inputs = len(normalized_idents)

    works: Dict[str, dict] = {}
    missed = 0

    futures_in = {executor.submit(
        net_fetch_work, ident): ident for ident in normalized_idents}

    emitt({"processed_works": 0, "remaining_works": total_inputs,
          "references_processed": 0})

    processed_inputs = 0
    for fut in as_completed(futures_in):
        ident = futures_in[fut]
        try:
            w = fut.result()
        except Exception:
            w = None

        processed_inputs += 1
        if not w:
            missed += 1
        else:
            wid = w["id"].rsplit("/", 1)[-1]  # bare W-id
            works[wid] = w

        emitt({
            "processed_works": processed_inputs,
            "remaining_works": total_inputs - processed_inputs,
            "references_processed": 0
        })

    # -----------------------------------
    # Phase 2a: build consistent work node IDs (FIX)
    # -----------------------------------
    # Each input work gets ONE node id. If it has a DOI, we use "doi:<doi_url>", else the W-id.
    # All links referencing this work MUST use this exact id as 'source'.
    work_node_id: Dict[str, str] = {}
    keyword_counter = Counter()
    for wid, w in works.items():
        doi_url = (w.get("ids") or {}).get("doi")
        node_id = f"doi:{doi_url}" if doi_url else wid
        work_node_id[wid] = node_id
        keyword_counter.update(set(net_extract_keywords(w)))

    # -------------------------
    # Phase 2b: collect FORWARD references (counts and raw links)
    # -------------------------
    counts_forward = Counter()
    links_forward: List[Dict[str, str]] = []

    for wid, w in works.items():
        refs = set(net_referenced_ids(w))  # set: referenced W-ids
        # FIX: exact node id used as link source
        src = work_node_id[wid]
        for rid in refs:
            counts_forward[rid] += 1
            links_forward.append(
                {"source": src, "target": rid, "kind": "forward"})

    # -------------------------
    # Phase 2c: collect BACKWARD references (citing works)
    # -------------------------
    # For each input work wid, fetch works that cite wid: Works().filter(cites=wid)
    # Aggregate per citing work ID how many input works it cites; keep >= min_count.
    def _fetch_citers_of_wid(wid: str, limit: int) -> Set[str]:
        """Return a set of W-ids of works that cite wid (capped to 'limit')."""
        try:
            # net_fetch_work_list_citers is not given; implement inline via pyalex Works() if available
            # Use server-side filtering: 'cites' returns works whose referenced_works includes wid
            # Paginate up to 'limit'
            citer_ids: Set[str] = set()

            # Simple capped iteration
            for item in list(Works().filter(cites=f"https://openalex.org/{wid}").get())[:200]:
                if isinstance(item, dict) and "id" in item:
                    citer_ids.add(item["id"].rsplit("/", 1)[-1])
            return citer_ids
        except Exception as e:
            print(e)
            return set()

    futures_citers = {executor.submit(
        _fetch_citers_of_wid, wid, cites_limit_per_work): wid for wid in works.keys()}
    counts_back = Counter()
    raw_backlinks: List[Tuple[str, str]] = []  # (work_node_id, citing_wid)

    for fut in as_completed(futures_citers):
        wid = futures_citers[fut]
        try:
            citers = fut.result()
        except Exception:
            citers = set()

        # use the exact node id of the input work for links
        src = work_node_id[wid]
        # Unique per input work to avoid double counting within the same citing list
        for citer_wid in set(citers):
            counts_back[citer_wid] += 1
            raw_backlinks.append((src, citer_wid))

        # progress (approximate)
        emitt({
            "processed_works": len(works) + missed,
            "remaining_works": 0,
            "references_processed": len(counts_forward) + len(counts_back)
        })

    # -------------------------
    # Phase 3: build "work" nodes (inputs)
    # -------------------------
    nodes: List[Dict[str, object]] = []
    references_processed = 0

    for wid, w in works.items():
        title, authors, venue, doi_url, openalex_url = net_work_metadata(w)
        # FIX: reuse the canonical id we decided earlier
        node_id = work_node_id[wid]
        nodes.append({
            "id": node_id,
            "type": "work",
            "title": title,
            "authors": authors,
            "venue": venue,
            "doi": doi_url,
            "openalex": openalex_url,
        })
        references_processed = len(counts_forward) + len(nodes)
        emitt({
            "processed_works": len(works) + missed,
            "remaining_works": 0,
            "references_processed": references_processed
        })

    # -------------------------
    # Phase 4a: fetch retained FORWARD reference works in parallel
    # -------------------------
    retained_forward_rids: List[str] = [
        rid for rid, c in counts_forward.items()
        if c >= min_count and rid != "W4285719527"
    ]
    futures_refs_fwd = {executor.submit(
        net_fetch_work, rid): rid for rid in retained_forward_rids}

    added_refs_fwd = 0
    for fut in as_completed(futures_refs_fwd):
        rid = futures_refs_fwd[fut]
        try:
            w = fut.result()
        except Exception:
            w = None

        if w:
            title, authors, venue, doi_url, openalex_url = net_work_metadata(w)
            nodes.append({
                "id": rid,
                "type": "ref",  # forward reference (center cluster in UI)
                "title": title,
                "authors": authors,
                "venue": venue,
                "doi": doi_url,
                "openalex": openalex_url,
                "count": counts_forward[rid],
            })
            keyword_counter.update(set(net_extract_keywords(w)))
        added_refs_fwd += 1
        emitt({
            "processed_works": len(works) + missed,
            "remaining_works": 0,
            "references_processed": sum(counts_forward.values()) + len(nodes) + added_refs_fwd
        })

    # -------------------------
    # Phase 4b: fetch retained BACKWARD reference works in parallel (NEW)
    # -------------------------
    retained_back_rids: List[str] = [
        rid for rid, c in counts_back.items()
        if rid != "W4285719527"
    ]
    futures_refs_back = {executor.submit(
        net_fetch_work, rid): rid for rid in retained_back_rids}

    added_refs_back = 0
    for fut in as_completed(futures_refs_back):
        rid = futures_refs_back[fut]
        try:
            w = fut.result()
        except Exception:
            w = None

        if w:
            title, authors, venue, doi_url, openalex_url = net_work_metadata(w)
            nodes.append({
                "id": rid,
                "type": "ref_back",  # backward reference (outside ring in UI)
                "title": title,
                "authors": authors,
                "venue": venue,
                "doi": doi_url,
                "openalex": openalex_url,
                "count": counts_back[rid],
            })
            keyword_counter.update(set(net_extract_keywords(w)))
        added_refs_back += 1
        emitt({
            "processed_works": len(works) + missed,
            "remaining_works": 0,
            "references_processed": sum(counts_forward.values()) + sum(counts_back.values()) + len(nodes) + added_refs_back + added_refs_fwd
        })

    # -------------------------
    # Phase 5: filter/assemble links
    # -------------------------
    # Keep only links that target retained forward/backward refs
    ref_fwd_kept: Set[str] = {n["id"] for n in nodes if n.get("type") == "ref"}
    ref_back_kept: Set[str] = {n["id"]
                               for n in nodes if n.get("type") == "ref_back"}

    # Forward links
    links_fwd = [dict(source=src_tgt["source"], target=src_tgt["target"], kind="forward")
                 for src_tgt in links_forward if src_tgt["target"] in ref_fwd_kept]

    # Backward links: build from raw_backlinks, keep only targets we retained
    links_back = [dict(source=src, target=citer, kind="back")
                  for (src, citer) in raw_backlinks if citer in ref_back_kept]

    links = links_fwd + links_back

    # -------------------------
    # Return graph
    # -------------------------
    top_keywords = dict(keyword_counter.most_common(200))
    return {
        "nodes": nodes,
        "links": links,
        "keywords": top_keywords,
        "meta": {
            "generated_at": datetime.date.today().isoformat(),
            "min_count": min_count,
            "input_size": len(dois_or_ids),
            "works_kept": len(works),
            "refs_kept_forward": len(ref_fwd_kept),
            "refs_kept_backward": len(ref_back_kept),
            "cites_limit_per_work": cites_limit_per_work,
            "keywords": top_keywords,
        },
    }
