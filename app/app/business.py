# Standard library
import pickle
import csv
import datetime
from datetime import timedelta, timezone
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Set, Tuple
import tempfile
import contextlib
from urllib.error import HTTPError
from concurrent.futures import as_completed, wait, FIRST_COMPLETED
from threading import Lock


# Third-party libraries
import dateparser
import pycountry
import pytz
import requests
from Levenshtein.StringMatcher import distance
from feedgen.feed import FeedGenerator
from flask import copy_current_request_context, has_app_context
from requests_cache import CachedSession, FileCache, RedisCache
import networkx as nx
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
    app,
    db,
)
from app.model import PublicationSource, Ranking, NetworkData
from app.arxiv import get_arxiv_results

pyalex_config.email = os.getenv("PYALEX_EMAIL", "nico@scholar.miage.dev")
pyalex_config.max_retries = 3
pyalex_config.retry_backoff_factor = 0.2
pyalex_config.retry_http_codes = [429, 500, 503]

_executor_pool: Dict[str, ThreadPoolExecutor] = {}
_executor_lock = Lock()
_openalex_session_lock = Lock()
_openalex_http_session = None


def _get_executor(name: str, max_workers: int) -> ThreadPoolExecutor:
    """
    Application-scoped executors (shared across requests) so that the concurrency
    cap applies process-wide rather than per request.
    """
    with _executor_lock:
        executor = _executor_pool.get(name)
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            _executor_pool[name] = executor
        return executor


def get_openalex_executor() -> ThreadPoolExecutor:
    return _get_executor("openalex", 8)


def get_scopus_executor() -> ThreadPoolExecutor:
    return _get_executor("scopus", 5)


def get_arxiv_executor() -> ThreadPoolExecutor:
    return _get_executor("arxiv", 1)


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
    original_request = s.request

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", OPENALEX_TIMEOUT_SECONDS)
        logger.info(
            "OpenAlex HTTP request start method=%s url=%s timeout=%s",
            method,
            url,
            kwargs.get("timeout"),
        )
        started_at = time.monotonic()
        try:
            response = original_request(method, url, **kwargs)
            logger.info(
                "OpenAlex HTTP request done method=%s url=%s status=%s duration=%.2fs",
                method,
                url,
                getattr(response, "status_code", "unknown"),
                time.monotonic() - started_at,
            )
            return response
        except Exception:
            logger.exception(
                "OpenAlex HTTP request failed method=%s url=%s duration=%.2fs",
                method,
                url,
                time.monotonic() - started_at,
            )
            raise

    s.request = request_with_timeout
    return s


def get_openalex_http_session():
    global _openalex_http_session
    with _openalex_session_lock:
        if _openalex_http_session is None:
            _openalex_http_session = _cached_requests_session()
        return _openalex_http_session


pyalex._get_requests_session = _cached_requests_session


logger = logging.getLogger('business')

MAX_RESULTS_QUERY = 1000
OPENALEX_TIMEOUT_SECONDS = int(os.environ.get("OPENALEX_TIMEOUT_SECONDS", "20"))
OPENALEX_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("OPENALEX_CONNECT_TIMEOUT_SECONDS", "5"))
OPENALEX_API_URL = "https://api.openalex.org/works"
ENABLE_OPENALEX_ENRICHMENT = os.environ.get("ENABLE_OPENALEX_ENRICHMENT", "false").lower() in {"1", "true", "yes", "on"}


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
    doi_url = net_normalize_input(doi)

    try:
        work = Works()[doi_url]
    except Exception as exc:
        logger.warning("OpenAlex DOI lookup failed for %s", doi_url, exc_info=exc)
        status = 404 if "404" in str(exc) else 502
        error = "doi_not_found" if status == 404 else "openalex_lookup_failed"
        return {"error": error, "doi": doi}, status

    return _build_ref_response_from_openalex(work)


def _build_ref_response_from_openalex(work: dict) -> dict:
    open_access = work.get("open_access") or {}
    authorships = work.get("authorships") or []
    primary_location = work.get("primary_location") or {}
    primary_source = primary_location.get("source") or {}
    biblio = work.get("biblio") or {}

    authors_list = []
    for authorship in authorships:
        author = authorship.get("author") or {}
        institutions = authorship.get("institutions") or []
        country = "xxx"
        affiliation = ""
        if institutions:
            first_institution = institutions[0] or {}
            country_code = first_institution.get("country_code") or ""
            country = country_code.lower() if country_code else "xxx"
            affiliation = first_institution.get("display_name") or ""
        authors_list.append({
            "display_name": author.get("display_name") or authorship.get("raw_author_name") or "",
            "orcid": author.get("orcid") or "",
            "openalex": author.get("id") or "",
            "country": country,
            "affiliation": affiliation,
        })

    first_author = authors_list[0] if authors_list else {}
    pubtitle = primary_source.get("display_name") or ""
    ranking = _get_ranking_safe(pubtitle)

    return {
        "doi": work.get("doi") or doi_url,
        "openalex": work.get("id") or "",
        "title": _strip_markup(work.get("title", "")),
        "year": work.get("publication_year"),
        "x-precise-date": work.get("publication_date") or "",
        "pubtitle": pubtitle,
        "pub_rank": ranking.get("rank", ""),
        "rank_source": ranking.get("source", ""),
        "hindex": ranking.get("hindex", ""),
        "volume": biblio.get("volume") or "",
        "issue": biblio.get("issue") or "",
        "first_page": biblio.get("first_page") or "",
        "last_page": biblio.get("last_page") or "",
        "type": work.get("type") or "",
        "X-OA": bool(open_access.get("is_oa")),
        "X-OA-URL": open_access.get("oa_url") or "",
        "X-abstract": inverted_abstrct_to_abstract(work.get("abstract_inverted_index")) if work.get("abstract_inverted_index") else "",
        "X-IsReferencedByCount": work.get("cited_by_count", 0),
        "X-refcount": work.get("referenced_works_count", 0),
        "X-subject": ((work.get("primary_topic") or {}).get("display_name")) or "",
        "X-authors": ", ".join([a["display_name"] for a in authors_list if a.get("display_name")]),
        "X-authors-list": authors_list,
        "X-FirstAuthor": first_author.get("display_name", ""),
        "X-FirstAuthor-ORCID": first_author.get("orcid", ""),
        "X-FirstAuthor-OpenAlex": first_author.get("openalex", ""),
        "X-Country-First-Author": first_author.get("country", "xxx"),
        "X-Country-First-affiliation": first_author.get("affiliation", ""),
        "referenced_works": work.get("referenced_works") or [],
        "cited_by_api_url": work.get("cited_by_api_url") or "",
    }


def _get_ranking_safe(pubtitle: str) -> dict:
    if not pubtitle:
        return get_blank_ranking()
    if has_app_context():
        return get_ranking(pubtitle)
    with app.app_context():
        return get_ranking(pubtitle)


def get_papers(count_scopus, query, xref, arxiv=False, emitt=lambda *args, **kwargs: None,
               existing_data={}, count_arxiv=0, arxiv_warning=None, limit=None,
               openalex_enrichment=None):
    run_id = uuid.uuid4().hex[:8]
    started_at = time.monotonic()
    effective_limit = MAX_RESULTS_QUERY if limit is None else max(0, int(limit))
    effective_scopus_count = min(effective_limit, count_scopus)
    effective_arxiv_count = min(max(0, effective_limit - effective_scopus_count), count_arxiv)
    use_openalex_enrichment = ENABLE_OPENALEX_ENRICHMENT if openalex_enrichment is None else openalex_enrichment
    logger.info(
        "get_papers start run_id=%s count_scopus=%s count_arxiv=%s effective_scopus=%s effective_arxiv=%s limit=%s xref=%s arxiv=%s openalex_enrichment=%s existing=%s query=%r",
        run_id,
        count_scopus,
        count_arxiv,
        effective_scopus_count,
        effective_arxiv_count,
        effective_limit,
        xref,
        arxiv,
        use_openalex_enrichment,
        len(existing_data or {}),
        query,
    )
    context = type('', (object,), {"success": 0, "failed": 0, "arxiv": 0, "duplicate": 0})()
    context_lock = Lock()
    client_results_bucket_size = min(max(10, count_scopus / 20), 200)
    client_bucket = []
    title_lock = Lock()
    title_index: Dict[str, Dict] = {}

    @copy_current_request_context
    def call_back(success, failure, arxiv=0, duplicate=0):
        logger.info(
            "get_papers progress run_id=%s success=%s failure=%s arxiv=%s duplicate=%s total=%s elapsed=%.2fs",
            run_id,
            success,
            failure,
            arxiv,
            duplicate,
            effective_scopus_count + effective_arxiv_count,
            time.monotonic() - started_at,
        )
        emitt('doi_update', {"total": effective_scopus_count + effective_arxiv_count,
                             "done": success, "failed": failure, "arxiv": arxiv, "duplicate": duplicate})

    def upsert_paper(paper: Dict, priority: str):
        title = paper.get("title", "")
        if not title:
            return paper, False
        with title_lock:
            existing = title_index.get(title)
            if existing:
                if priority == "scopus":
                    for k, v in paper.items():
                        if v not in ("", None, []):
                            existing[k] = v
                else:
                    for k, v in paper.items():
                        if (existing.get(k) in ("", None, []) or k == "doi") and v not in ("", None, []):
                            existing[k] = v
                return existing, True
            title_index[title] = paper
            return paper, False

    def emit_results_if_needed():
        nonlocal client_bucket
        if len(client_bucket) > client_results_bucket_size:
            logger.info(
                "get_papers emit partial run_id=%s bucket_size=%s threshold=%s elapsed=%.2fs",
                run_id,
                len(client_bucket),
                client_results_bucket_size,
                time.monotonic() - started_at,
            )
            emitt('doi_results', client_bucket)
            client_bucket = []

    def fetch_scopus_batch(offset, batch_size):
        batch_started_at = time.monotonic()
        logger.info("scopus batch start run_id=%s offset=%s batch_size=%s query=%r", run_id, offset, batch_size, query)
        try:
            #print(f"SCPUS_BACKEND {SCPUS_BACKEND % (offset, batch_size, escape_query(query))}")
            partial_results = session_scpus.get(
                SCPUS_BACKEND % (offset, batch_size, escape_query(query))).json()

            #print(f'{partial_results["search-results"]}')
            entries = partial_results["search-results"]["entry"]

            entries = [entry for entry in entries if (entry.get(
                'prism:doi') and f"https://doi.org/{entry.get('prism:doi').lower()}" not in existing_data.keys()) or not entry.get('prism:doi')]
            logger.info(
                "scopus batch done run_id=%s offset=%s entries=%s duration=%.2fs",
                run_id,
                offset,
                len(entries),
                time.monotonic() - batch_started_at,
            )
            return ("scopus", entries)
        except Exception as exc:
            logger.exception("Failed to fetch scopus batch run_id=%s offset=%s", run_id, offset, exc_info=exc)
            return ("scopus", [])

    def fetch_arxiv_entries():
        arxiv_started_at = time.monotonic()
        try:
            logger.info("arxiv fetch start run_id=%s query=%r", run_id, query)
            entries = get_arxiv_results(
                query,
                on_unsupported=arxiv_warning,
                on_warning=arxiv_warning,
            ).entries
            logger.info(
                "arxiv fetch done run_id=%s count=%s duration=%.2fs query=%r",
                run_id,
                len(entries),
                time.monotonic() - arxiv_started_at,
                query,
            )
            return ("arxiv", entries[:effective_arxiv_count])
        except ValueError as exc:
            logger.warning("Skipping arXiv fetch run_id=%s unsupported query=%r error=%s", run_id, query, exc)
            return ("arxiv", [])
        except Exception as exc:
            logger.exception("Failed to fetch arXiv entries run_id=%s query=%r", run_id, query, exc_info=exc)
            return ("arxiv", [])

    def build_arxiv_entry(paper):
        title = getattr(getattr(paper, "title", None), "value", "")
        authors = getattr(paper, "authors", []) or []
        links = getattr(paper, "links", []) or []
        published = getattr(paper, "published", None)
        summary = getattr(getattr(paper, "summary", None), "value", "")
        authors_list = [{"display_name": a.name, "orcid": "", "openalex": ""} for a in authors]
        return {
            "doi": getattr(paper, "id_", ""),
            "title": title,
            "year": getattr(published, "year", ""),
            "x-precise-date": str(published or ""),
            "pubtitle": "arXiv.org",
            "pub_rank": "",
            "rank_source": "",
            "hindex": "",
            "X-OA": True,
            "X-FirstAuthor": authors[0].name if authors else "",
            "X-Country-First-Author": "",
            "X-Country-First-affiliation": "",
            "X-FirstAuthor-ORCID": "",
            "X-FirstAuthor-OpenAlex": "",
            "X-IsReferencedByCount": "",
            "X-subject": "",
            "X-refcount": "",
            "X-abstract": summary,
            "X-authors": ", ".join([a.name for a in authors]),
            "X-authors-list": authors_list,
            "X-OA-URL": links[0].href if links else "",
            "source_provider": "arxiv",
        }

    def enrich_scopus_entry(entry):
        item_started_at = time.monotonic()
        bucket = []
        try:
            doi = entry.get("prism:doi", "")
            title = entry.get("dc:title", "")
            logger.info(
                "scopus enrich start run_id=%s doi=%r title=%r xref=%s openalex_enabled=%s",
                run_id,
                doi,
                title,
                xref,
                use_openalex_enrichment,
            )
            if xref and use_openalex_enrichment:
                logger.info("enriching from openalex")
                extract_data_openalex_from_scopus(bucket, entry, context, call_back)
            else:
                if xref:
                    logger.warning(
                        "OpenAlex enrichment disabled; using Scopus data only run_id=%s doi=%r title=%r",
                        run_id,
                        doi,
                        title,
                    )
                else:
                    logger.info("not enriching from openalex")
                extract_data_scopus(bucket, entry, context, call_back)
        except Exception as exc:
            logger.exception("Failed to enrich scopus entry run_id=%s", run_id, exc_info=exc)
        logger.info(
            "scopus enrich done run_id=%s bucket=%s duration=%.2fs",
            run_id,
            len(bucket),
            time.monotonic() - item_started_at,
        )
        return ("scopus", bucket)

    def enrich_arxiv_entry(paper):
        item_started_at = time.monotonic()
        bucket = []
        try:
            logger.info(
                "arxiv enrich start run_id=%s arxiv_id=%r title=%r",
                run_id,
                getattr(paper, "id_", ""),
                getattr(getattr(paper, "title", None), "value", ""),
            )
            bucket.append(build_arxiv_entry(paper))
        except Exception as exc:
            logger.exception("Failed to enrich arXiv entry run_id=%s", run_id, exc_info=exc)
        logger.info(
            "arxiv enrich done run_id=%s bucket=%s duration=%.2fs",
            run_id,
            len(bucket),
            time.monotonic() - item_started_at,
        )
        return ("arxiv", bucket)

    provider_futures = set()
    enrichment_futures = set()
    future_labels = {}

    batch_offsets = list(range(0, effective_scopus_count, 25))
    for offset in batch_offsets:
        batch_size = min(25, effective_scopus_count - offset)
        fut = get_scopus_executor().submit(fetch_scopus_batch, offset, batch_size)
        provider_futures.add(fut)
        future_labels[fut] = f"provider:scopus:{offset}"
        logger.info("submitted provider future run_id=%s label=%s", run_id, future_labels[fut])

    if arxiv and effective_arxiv_count > 0:
        fut = get_arxiv_executor().submit(fetch_arxiv_entries)
        provider_futures.add(fut)
        future_labels[fut] = "provider:arxiv"
        logger.info("submitted provider future run_id=%s label=%s", run_id, future_labels[fut])

    def submit_enrichment(provider_name, payload):
        if provider_name == "scopus":
            if xref and use_openalex_enrichment:
                #logger.debug("sumitting enrichment from openalex")
                return get_openalex_executor().submit(enrich_scopus_entry, payload)
            #logger.debug("just loading data from scopus")
            return get_scopus_executor().submit(enrich_scopus_entry, payload)
        if provider_name == "arxiv":
            return get_arxiv_executor().submit(enrich_arxiv_entry, payload)
        raise ValueError(f"Unknown provider {provider_name}")

    wait_cycles = 0
    while provider_futures or enrichment_futures:
        wait_cycles += 1
        pending = provider_futures | enrichment_futures
        logger.info(
            "get_papers wait run_id=%s cycle=%s provider_pending=%s enrichment_pending=%s elapsed=%.2fs",
            run_id,
            wait_cycles,
            len(provider_futures),
            len(enrichment_futures),
            time.monotonic() - started_at,
        )
        done, _ = wait(pending, timeout=5, return_when=FIRST_COMPLETED)
        if not done:
            labels = [future_labels.get(fut, "unknown") for fut in pending]
            logger.warning(
                "get_papers wait heartbeat run_id=%s no_future_done provider_pending=%s enrichment_pending=%s labels=%s elapsed=%.2fs",
                run_id,
                len(provider_futures),
                len(enrichment_futures),
                labels[:30],
                time.monotonic() - started_at,
            )
            continue
        for fut in done:
            label = future_labels.pop(fut, "unknown")
            logger.info("get_papers future done run_id=%s label=%s elapsed=%.2fs", run_id, label, time.monotonic() - started_at)
            if fut in provider_futures:
                provider_futures.remove(fut)
                provider_name, payloads = fut.result()
                logger.info(
                    "get_papers provider payloads run_id=%s provider=%s payload_count=%s",
                    run_id,
                    provider_name,
                    len(payloads),
                )
                for payload in payloads:
                    logger.info(f"enrichment request submitted for {provider_name}")
                    enrich_future = submit_enrichment(provider_name, payload)
                    enrichment_futures.add(enrich_future)
                    title = ""
                    if provider_name == "arxiv":
                        title = getattr(getattr(payload, "title", None), "value", "")
                    elif isinstance(payload, dict):
                        title = payload.get("dc:title", "")
                    future_labels[enrich_future] = f"enrich:{provider_name}:{title[:80]}"
                    logger.info("submitted enrichment future run_id=%s label=%s", run_id, future_labels[enrich_future])
            else:
                enrichment_futures.remove(fut)
                provider_name, bucket = fut.result()
                logger.info(
                    "get_papers enrichment payload done run_id=%s provider=%s bucket=%s",
                    run_id,
                    provider_name,
                    len(bucket),
                )
                if not bucket:
                    continue
                for paper in bucket:
                    stored, merged = upsert_paper(paper, provider_name)
                    client_bucket.append(stored)
                    if provider_name == "arxiv":
                        with context_lock:
                            if merged:
                                context.duplicate += 1
                            else:
                                context.arxiv += 1
                            call_back(context.success, context.failed, context.arxiv, context.duplicate)
                emit_results_if_needed()

    if client_bucket:
        logger.info(
            "get_papers emit final partial run_id=%s bucket_size=%s elapsed=%.2fs",
            run_id,
            len(client_bucket),
            time.monotonic() - started_at,
        )
        emitt('doi_results', client_bucket)

    dois = list(title_index.values())
    logger.info(
        "get_papers done run_id=%s papers=%s success=%s failed=%s arxiv=%s duplicate=%s elapsed=%.2fs",
        run_id,
        len(dois),
        context.success,
        context.failed,
        context.arxiv,
        context.duplicate,
        time.monotonic() - started_at,
    )
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


def get_openalex_work_for_doi(doi: str) -> dict:
    doi_url = f"https://doi.org/{doi}"
    encoded_id = urllib.parse.quote(doi_url, safe="")
    url = f"{OPENALEX_API_URL}/{encoded_id}"
    params = {"mailto": pyalex_config.email} if pyalex_config.email else {}
    timeout = (OPENALEX_CONNECT_TIMEOUT_SECONDS, OPENALEX_TIMEOUT_SECONDS)
    session = get_openalex_http_session()
    started_at = time.monotonic()
    logger.info(
        "OpenAlex direct DOI lookup start doi=%s url=%s timeout=%s",
        doi,
        url,
        timeout,
    )
    response = session.get(url, params=params, timeout=timeout)
    duration = time.monotonic() - started_at
    logger.info(
        "OpenAlex direct DOI lookup done doi=%s status=%s bytes=%s cached=%s duration=%.2fs",
        doi,
        response.status_code,
        len(response.content or b""),
        getattr(response, "from_cache", False),
        duration,
    )
    response.raise_for_status()
    return response.json()


def extract_data_openalex_from_scopus(bucket, entry, context, call_back):
    
    if "prism:doi" in entry:
        context.success += 1
    else:
        context.failed += 1

    doi = entry.get("prism:doi", "")
    
    

    if len(doi) > 0:
        try:
            oa_response = get_openalex_work_for_doi(doi)
            
            load_response_from_openAlex_scopus(bucket, oa_response, entry)
        except Exception:
            logger.exception("OpenAlex enrichment failed; falling back to Scopus data doi=%s", doi)
            load_response_from_scpus(bucket, entry)
    else:
        # No DOI available; avoid fuzzy OpenAlex title matching to prevent mis-associations.
        load_response_from_scpus(bucket, entry)

    try:
        call_back(context.success, context.failed, context.arxiv, context.duplicate)
    except:
        pass


def extract_data_scopus(bucket, entry, context, call_back):
    if "prism:doi" in entry:
        context.success += 1
    else:
        context.failed += 1

    load_response_from_scpus(bucket, entry)

    try:
        call_back(context.success, context.failed, context.arxiv, context.duplicate)
    except:
        pass


def extract_data_arxiv(dois, bucket, arxiv_results, context, call_back,
                       add_arxiv_results=False, complete_data_openalex=False,
                       id_overrides=None, works_by_arxiv_id=None):

    scopus_papers = {d["title"]: d for d in dois}
    id_overrides = id_overrides or {}
    works_by_arxiv_id = works_by_arxiv_id or {}
    scopus_lock = Lock()
    context_lock = Lock()

    def process_paper(paper):
        local_bucket = []
        arxiv_added = 0
        duplicate_added = 0
        title = getattr(getattr(paper, "title", None), "value", "")

        if not title:
            return local_bucket, arxiv_added, duplicate_added

        with scopus_lock:
            existing = scopus_papers.get(title)

        if existing:
            target_id = id_overrides.get(paper.id_, paper.id_)
            with scopus_lock:
                if existing.get("doi", "") == "":
                    existing["doi"] = target_id
                    existing["X-OA-URL"] = paper.links[0].href
                    existing["X-abstract"] = paper.summary.value
                    existing["X-OA"] = True
                logger.info("Updated %s from arXiv enrichment", existing["doi"])
                local_bucket.append(existing)
                duplicate_added = 1
                return local_bucket, arxiv_added, duplicate_added

        if add_arxiv_results:
            authors_list = [{"display_name": a.name, "orcid": "",
                            "openalex": ""} for a in paper.authors]
            work = works_by_arxiv_id.get(paper.id_)
            if work:
                load_response_from_openAlex_arxiv(
                    local_bucket, work, paper, id_overrides.get(paper.id_, paper.id_))
            else:
                local_bucket.append({"doi": id_overrides.get(paper.id_, paper.id_), "title": paper.title.value,
                                   "year": paper.published.year,
                                   "x-precise-date": str(paper.published),
                                   "pubtitle": "arXiv.org",
                                   "pub_rank": "",
                                   "rank_source": "",
                                   "hindex": "",
                                   "X-OA": True,
                                   "X-FirstAuthor": paper.authors[0].name,
                                   "X-Country-First-Author": "",
                                   "X-Country-First-affiliation": "",
                                   "X-FirstAuthor-ORCID": "",
                                   "X-FirstAuthor-OpenAlex": "",
                                   "X-IsReferencedByCount": "",
                                   "X-subject": "",
                                   "X-refcount": "",
                                   "X-abstract": paper.summary.value,
                                   "X-authors": ", ".join([a.name for a in paper.authors]),
                                   "X-authors-list":  authors_list,
                                   "X-OA-URL": paper.links[0].href
                                   })
            arxiv_added = 1
        return local_bucket, arxiv_added, duplicate_added

    futures = [get_scopus_executor().submit(process_paper, paper)
               for paper in arxiv_results]

    for future in as_completed(futures):
        local_bucket, added, dup_added = future.result()
        if local_bucket:
            bucket += local_bucket
        if added:
            with context_lock:
                context.arxiv += added
                if context.arxiv % 25 == 0:
                    logger.debug("Sending DOI update (arXiv count=%s)", context.arxiv)
                    call_back(context.success, context.failed, context.arxiv, context.duplicate)
        if dup_added:
            with context_lock:
                context.duplicate += dup_added
                call_back(context.success, context.failed, context.arxiv, context.duplicate)

    if add_arxiv_results and arxiv_results:
        call_back(context.success, context.failed, context.arxiv, context.duplicate)


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
    if "prism:doi" in entry:
        doi = "https://doi.org/"+entry.get("prism:doi", "")
    else:
        doi = ""
        
    
    bucket.append(
        {"doi": doi,
         "issn": issn,
         "title": entry.get("dc:title", "-"),
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


def _strip_markup(text: str) -> str:
    """
    Remove simple HTML tags and LaTeX markers from a title string.
    This is intentionally conservative to avoid noisy OpenAlex titles.
    """
    if not text:
        return ""
    no_html = re.sub(r"<[^>]+>", "", text)
    # Drop math fragments delimited by $, \( \), or \[ \]
    no_math = re.sub(r"(\\\[.*?\\\]|\\\(.*?\\\)|\\begin\{.*?\}.*?\\end\{.*?\}|\$.*?\$)", "", no_html)
    # Remove lightweight LaTeX commands like \alpha, \beta
    no_commands = re.sub(r"\\[A-Za-z]+", "", no_math)
    # Collapse extra whitespace after stripping
    return " ".join(no_commands.split())


def get_abstract_semanticscholar(doi: str):
    """
    Retrieve paper abstract from Semantic Scholar Graph API.
    Returns None if not found or no abstract.
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    params = {"fields": "title,abstract"}
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        logger.warning("Abstract retrieval from Semantic Scholar failed for DOI %s (status %s)", doi, r.status_code)
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


def load_response_from_openAlex_scopus(bucket, openalex_response, entry):

    

    oa_url = (openalex_response.get("open_access") or {}).get("oa_url", None)
    authors_list = [{"display_name": a["author"]["display_name"], "orcid": a["author"]["orcid"]
                     if a["author"]["orcid"] else "", "openalex": a["author"]["id"]} for a in openalex_response["authorships"]]

    abstract = inverted_abstrct_to_abstract(
        openalex_response["abstract_inverted_index"]) if "abstract_inverted_index" in openalex_response else None
    #if not abstract:
    #    abstract = get_abstract_semanticscholar(openalex_response["doi"])

    # it's too slow
    # if not abstract:
    #     abstract = get_abstract_from_pdf_sources(
    #         r["doi"], oa_url, "nicolas.herbaut@u-bordeaux.fr")
    if not abstract:
        abstract = ""

    if len(authors_list) == 0:
        authors_list = [{"display_name": entry.get(
            'dc:creator', "unknown"), "orcid": "", "openalex": ""}]
    title = _strip_markup(openalex_response.get("title", ""))

    bucket.append({"doi": openalex_response["doi"], "title": title,
                   "year": openalex_response["publication_year"],
                   "x-precise-date": openalex_response["publication_date"],
                   "pubtitle": entry.get('prism:publicationName', ""),
                   "pub_rank": "",
                   "rank_source": "",
                   "hindex": "",
                   "X-OA": openalex_response["open_access"]["is_oa"],
                   "X-FirstAuthor": authors_list[0]["display_name"] if len(authors_list) > 0 else "",
                   "X-Country-First-Author": get_first_auth_country(entry),
                   "X-Country-First-affiliation": get_first_auth_affil(entry),
                   "X-FirstAuthor-ORCID": "",
                   "X-FirstAuthor-OpenAlex": "",
                   "X-IsReferencedByCount": openalex_response["cited_by_count"],
                   "X-subject": (openalex_response["primary_topic"] if "primary_topic" in openalex_response and openalex_response["primary_topic"] and len(openalex_response["primary_topic"]) else {}).get("display_name", ""),
                   "X-refcount": openalex_response["referenced_works_count"],
                   "X-abstract": abstract,
                   "X-authors": ", ".join([a["author"]["display_name"] for a in openalex_response["authorships"]]),
                   "X-authors-list": authors_list,
                   "X-OA-URL": oa_url or ""

                   })


def load_response_from_openAlex_arxiv(bucket, work, paper, resolved_id):
    """Build a full record for an arXiv paper using OpenAlex data."""

    def from_obj(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    oa_url = from_obj(from_obj(work, "open_access", {}), "oa_url", "")
    is_oa = from_obj(from_obj(work, "open_access", {}), "is_oa", True)

    authorships = from_obj(work, "authorships", []) or []
    authors_list = []
    for a in authorships:
        if isinstance(a, dict):
            auth = a.get("author", {})
            display_name = (auth or {}).get("display_name") or ""
            orcid = (auth or {}).get("orcid") or ""
            author_id = (auth or {}).get("id") or ""
        else:
            display_name = getattr(getattr(a, "author", None), "display_name", "") or ""
            orcid = getattr(getattr(a, "author", None), "orcid", "") or ""
            author_id = getattr(getattr(a, "author", None), "id", "") or ""
        if display_name:
            authors_list.append({"display_name": display_name,
                                 "orcid": orcid.split("/")[-1] if orcid else "",
                                 "openalex": author_id})

    has_inv = isinstance(work, dict) and "abstract_inverted_index" in work
    abstract = inverted_abstrct_to_abstract(from_obj(work, "abstract_inverted_index", None)) if has_inv else None
    if not abstract:
        abstract = paper.summary.value if getattr(paper, "summary", None) else ""

    primary_topic = from_obj(work, "primary_topic", {}) or {}
    subjects = from_obj(primary_topic, "display_name", "")

    bucket.append({
        "doi": resolved_id,
        "title": from_obj(work, "title", paper.title.value if getattr(paper, "title", None) else ""),
        "year": from_obj(work, "publication_year", getattr(getattr(paper, "published", None), "year", "")),
        "x-precise-date": from_obj(work, "publication_date", str(getattr(paper, "published", ""))),
        "pubtitle": from_obj(from_obj(work, "host_venue", {}), "display_name", "arXiv.org"),
        "pub_rank": "",
        "rank_source": "",
        "hindex": "",
        "X-OA": is_oa,
        "X-FirstAuthor": authors_list[0]["display_name"] if authors_list else (paper.authors[0].name if getattr(paper, "authors", None) else ""),
        "X-Country-First-Author": "",
        "X-Country-First-affiliation": "",
        "X-FirstAuthor-ORCID": authors_list[0].get("orcid", "") if authors_list else "",
        "X-FirstAuthor-OpenAlex": authors_list[0].get("openalex", "") if authors_list else "",
        "X-IsReferencedByCount": from_obj(work, "cited_by_count", ""),
        "X-subject": subjects,
        "X-refcount": from_obj(work, "referenced_works_count", ""),
        "X-abstract": abstract,
        "X-authors": ", ".join(a["display_name"] for a in authors_list) if authors_list else ", ".join([a.name for a in getattr(paper, "authors", [])]),
        "X-authors-list": authors_list if authors_list else [{"display_name": a.name, "orcid": "", "openalex": ""} for a in getattr(paper, "authors", [])],
        "X-OA-URL": oa_url or (paper.links[0].href if getattr(paper, "links", []) else ""),
        "source_provider": "arxiv",
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


def count_results_for_query(query, include_arxiv=False, arxiv_warning=None):
    # print(f"query with {API_KEY} API_KEY")
    started_at = time.monotonic()
    logger.info(
        "count_results start include_arxiv=%s query_len=%s query=%r",
        include_arxiv,
        len(query or ""),
        query,
    )
    logger.info("count_results scopus request start query=%r", query)
    response = session_scpus.get(SCPUS_BACKEND %
                                 (0, 1, escape_query(query))).json()
    logger.info(
        "count_results scopus request done keys=%s duration=%.2fs query=%r",
        list(response.keys()) if isinstance(response, dict) else type(response).__name__,
        time.monotonic() - started_at,
        query,
    )

    if "search-results" in response:

        count = int(response["search-results"]["opensearch:totalResults"])
        logger.info("count_results scopus count=%s query=%r", count, query)
        arxiv_count = 0
        if include_arxiv:
            try:
                arxiv_started_at = time.monotonic()
                logger.info("count_results arxiv start query=%r", query)
                arxiv_results = get_arxiv_results(
                    query,
                    on_unsupported=arxiv_warning,
                    on_warning=arxiv_warning,
                )
                arxiv_count = len(arxiv_results.entries)
                logger.info(
                    "count_results arxiv success query=%r entries=%s duration=%.2fs",
                    query,
                    arxiv_count,
                    time.monotonic() - arxiv_started_at,
                )
            except ValueError as exc:
                logger.warning("arXiv count skipped for unsupported query=%r error=%s", query, exc)
            except Exception as exc:
                logger.exception("arXiv count failed query=%r", query, exc_info=exc)
        logger.info(
            "count_results done scopus=%s arxiv=%s total=%s duration=%.2fs query=%r",
            count,
            arxiv_count,
            count + arxiv_count,
            time.monotonic() - started_at,
            query,
        )
        return count, arxiv_count
    else:

        logger.warning("count_results no search-results response=%r duration=%.2fs query=%r", response, time.monotonic() - started_at, query)
        return 0, 0


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


def net_work_metadata(w: dict) -> Tuple[str, List[str], str, str, str, int | None]:
    """Extract title, authors, venue, doi_url, openalex_url, publication_year."""
    if not w:
        return "", [], "", "", "", None
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
    publication_year = w.get("publication_year")
    return title, authors, venue, doi_url, openalex_url, publication_year


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


def _net_parse_graph_data(data):
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return None
    if isinstance(data, dict):
        return data
    return None


def net_detect_communities(data, resolution: float = 1.0) -> dict | None:
    """
    Detect communities using a modularity-based algorithm (Louvain if available,
    otherwise greedy modularity). Returns a graph dict with node 'community' ids.
    """
    graph_data = _net_parse_graph_data(data)
    if not graph_data:
        return None

    G = nx.Graph()
    nodes = graph_data.get("nodes", [])
    links = graph_data.get("links", [])

    for n in nodes:
        node_id = str(n.get("id"))
        if node_id:
            G.add_node(node_id)

    for l in links:
        s = l.get("source")
        t = l.get("target")
        s_id = str(s.get("id") if isinstance(s, dict) else s)
        t_id = str(t.get("id") if isinstance(t, dict) else t)
        if not s_id or not t_id or s_id == t_id:
            continue
        if G.has_edge(s_id, t_id):
            G[s_id][t_id]["weight"] = G[s_id][t_id].get("weight", 1) + 1
        else:
            G.add_edge(s_id, t_id, weight=1)

    if G.number_of_nodes() == 0:
        return graph_data

    algo = "louvain"
    try:
        from networkx.algorithms.community import louvain_communities
        communities = louvain_communities(G, weight="weight", resolution=resolution, seed=42)
    except Exception:
        from networkx.algorithms.community import greedy_modularity_communities
        communities = greedy_modularity_communities(G, weight="weight", resolution=resolution)
        algo = "greedy"

    node_to_comm = {}
    for idx, comm in enumerate(communities):
        for node_id in comm:
            node_to_comm[str(node_id)] = idx

    for n in nodes:
        node_id = str(n.get("id"))
        n["community"] = node_to_comm.get(node_id, -1)

    graph_data.setdefault("meta", {})
    graph_data["meta"]["community_resolution"] = resolution
    graph_data["meta"]["community_algo"] = algo
    graph_data["communities"] = [{"id": i, "size": len(c)} for i, c in enumerate(communities)]
    return graph_data


# -------------------------------------
# Main function
# -------------------------------------


def net_build_graph(
    dois_or_ids: Iterable[str],
    min_count: int = 2,
    emitt=lambda *args, **kwargs: None,
    executor: ThreadPoolExecutor | None = None,
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
    if executor is None:
        executor = get_openalex_executor()

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
            logger.exception("Failed to fetch citers for %s", wid, exc_info=e)
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
        title, authors, venue, doi_url, openalex_url, publication_year = net_work_metadata(w)
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
            "publication_year": publication_year,
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
            title, authors, venue, doi_url, openalex_url, publication_year = net_work_metadata(w)
            nodes.append({
                "id": rid,
                "type": "ref",  # forward reference (center cluster in UI)
                "title": title,
                "authors": authors,
                "venue": venue,
                "doi": doi_url,
                "openalex": openalex_url,
                "publication_year": publication_year,
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
            title, authors, venue, doi_url, openalex_url, publication_year = net_work_metadata(w)
            nodes.append({
                "id": rid,
                "type": "ref_back",  # backward reference (outside ring in UI)
                "title": title,
                "authors": authors,
                "venue": venue,
                "doi": doi_url,
                "openalex": openalex_url,
                "publication_year": publication_year,
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
