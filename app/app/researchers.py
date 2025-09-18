import json
import re
from collections import Counter
import os
import pyalex
from pyalex import (Authors, Funders, Institutions, Publishers, Sources,
                    Topics, Works, config)



pyalex.config.email = os.getenv("PYALEX_EMAIL","nico@scholar.miage.dev")
from app.cache import session_doi, session_orcid, session_xref

def lookup_doi_data(doi):
    url = "http://dx.doi.org/" + doi
    headers = {"accept": "application/json"}
    return session_doi.get(url, headers=headers).json()


def my_yield(arg):
    yield arg


_LEADING_PHRASES = [
    "International Conference",
    "International Symposium",
    "European Conference",
    "European Symposium",
    "ACM International Conference",
    "ACM Conference",
    "ACM Symposium",
    "ACM",
    "IEEE",
    "IEEE International Conference",
    "IEEE Conference",
    "IEEE Symposium",
    "IFIP International Conference",
    "SIAM Conference",
    "Workshop",
    "Conference",
    "Symposium",
]
# Articles/prepositions/conjunctions commonly ignored in acronyms.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "of", "on", "in", "to", "with", "from",
    "by", "at", "into", "via", "over", "between", "without", "within"
}

_ORDINAL_PREFIX = re.compile(r"^\s*(\d+)(?:st|nd|rd|th)\b", re.IGNORECASE)
# used to strip standalone years in titles
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_MULTI_WS = re.compile(r"\s+")
_PARENS = re.compile(r"\([^)]*\)")


def _strip_leading_phrases(title: str) -> str:
    t = title.lstrip()
    # Remove leading numeric ordinal like "17th" (and any following punctuation).
    t = _ORDINAL_PREFIX.sub("", t).lstrip(":,. -")
    # Remove a single leading generic phrase if present.
    for phrase in _LEADING_PHRASES:
        # optional “on/of/for” that often follows the phrase
        pat = re.compile(
            rf"^(?:{re.escape(phrase)})\b(?:\s+(?:on|of|for))?\s+", re.IGNORECASE)
        m = pat.match(t)
        if m:
            t = t[m.end():]
            break
    return t


def _compute_acronym_from_title(title: str) -> str | None:
    # Remove parenthetical segments that often contain locations or series info.
    t = _PARENS.sub(" ", title)
    # Remove standalone years anywhere in the remaining title.
    t = _YEAR.sub(" ", t)
    # Remove leading ordinal and generic phrases.
    t = _strip_leading_phrases(t)
    # Normalize whitespace and punctuation separators.
    t = _MULTI_WS.sub(" ", t.strip())
    # Tokenize on non-letters while keeping intra-word apostrophes/hyphens as separators.
    tokens = re.split(r"[^A-Za-z]+", t)
    # Keep capitalized words; if none, fallback to any words not in stopwords.
    caps = [w for w in tokens if w and w[0].isupper() and w.lower()
            not in _STOPWORDS]
    if caps:
        return "".join(w[0].upper() for w in caps)
    # Fallback: first letters of non-stopword words.
    words = [w for w in tokens if w and w.lower() not in _STOPWORDS]
    if words:
        return "".join(w[0].upper() for w in words)
    return None


def extract_acronym(event_name: str) -> str | None:
    # Case 1: starts with acronym (uppercase letters), optionally followed by digits (e.g., year).
    m = re.match(r"^([A-Z]{2,})(?:\d{2,4})?\b", event_name)
    if m:
        return m.group(1).upper()

    # Case 2: acronym inside parentheses anywhere.
    m = re.search(r"\(([A-Z0-9]{2,})\)", event_name)
    if m:
        return m.group(1).upper()

    # Case 3: acronym in caps followed by a quoted year.
    m = re.search(r"\b([A-Z]{2,})\s*['\"]\d{2,4}\b", event_name)
    if m:
        return m.group(1).upper()

    # Fallback: compute from title.
    return _compute_acronym_from_title(event_name)


def count_acronyms(event_names: list[str]) -> dict[str, int]:
    return dict(Counter(filter(None, (extract_acronym(s) for s in event_names))))


def get_venue_for_openalex(openalex_id, venue_callback=my_yield, author_callback=lambda *args, **kwargs: None):

    author = Authors()[openalex_id]
    author_callback(author["display_name"])
    venues = []
    dois = []
    bad = {}
    for work_page in Works().filter(authorships={"author": {"id": openalex_id}}).paginate(per_page=200):
        for work in work_page:
            if "doi" in work:
                dois.append(work["doi"])

    extract_doi_with_xref(venue_callback, dois, venues, bad)

    return venues


def get_venue_for_orcid(orcid, venue_callback=my_yield, author_callback=lambda *args, **kwargs: None):

    data = session_orcid.get(
        orcid, headers={"Accept": "application/json"}).json()

    author_callback(" ".join((data["person"]["name"]["given-names"]
                    ["value"], data["person"]["name"]["family-name"]["value"])))

    dois = [external_id["external-id-normalized"]["value"] for group in data["activities-summary"]["works"]["group"]
            if "external-ids" in group for external_id in group["external-ids"]["external-id"] if external_id["external-id-type"] == "doi"]

    venues = []
    bad = {}

    extract_doi_with_xref(venue_callback, dois, venues, bad)
    return venues


def extract_doi_with_xref(venue_callback, dois, venues, bad):
    for doi in dois:
        aka=""
        venue = None
        response = session_xref.get(f"https://api.crossref.org/works/{doi}")
        if response.status_code == 200:
            response_json = response.json()["message"]

            if "event" in response_json:
                venue = extract_acronym(response_json["event"]["name"])
                aka=response_json["event"]["name"]

            if not venue and "assertion" in response_json:
                conf_accr = [assertion["value"] for assertion in response_json["assertion"]
                             if assertion["name"] == "conference_acronym"]
                if len(conf_accr) > 0:
                    venue = conf_accr[0]
                    aka=conf_accr[0]
            if not venue and "short-container-title" in response_json and len(response_json["short-container-title"]) > 0:
                venue = response_json["short-container-title"][-1]
            if (not venue or len(venue) == 0) and "container-title" in response_json and len(response_json["container-title"]) > 0:
                venue = response_json["container-title"][-1]
            if not venue:
                bad[doi] = response_json
            else:
                venues.append(venue)
                publication_year = response_json["created"]["date-parts"][0][0]
                publication_title = response_json["title"]

                venue_callback(json.dumps(
                    {"venue": venue, "doi": doi, "publication_year": publication_year, "publication_title": publication_title, "aka":aka}))
