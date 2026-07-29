"""Клиенты и парсеры OpenAlex, ORCID, Crossref."""
from __future__ import annotations

import logging
import re
import time

import requests

from ..config import (
    CROSSREF_TIMEOUT,
    CROSSREF_URL,
    HTTP_TIMEOUT,
    OPENALEX_API_KEY,
    OPENALEX_AUTHORS_URL,
    ORCID_PUBLIC_API,
    TOPICS_LIMIT,
    USER_AGENT,
    USER_AGENT_EMAIL,
)
from .text import alpha, orcid_tail, tail_id

logger = logging.getLogger(__name__)


def crossref_authors(doi: str) -> list[tuple[str, str]]:
    """[(фамилия_alpha, orcid)] по DOI из Crossref."""
    doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
    try:
        r = requests.get(
            f"{CROSSREF_URL}{doi}",
            headers={"User-Agent": f"ITMO-Research/1.0 (mailto:{USER_AGENT_EMAIL})"},
            timeout=CROSSREF_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    try:
        authors = r.json()["message"].get("author", [])
    except (ValueError, KeyError):
        return []
    out = []
    for a in authors:
        orc = orcid_tail(a.get("ORCID"))
        fam = alpha(a.get("family", ""))
        if orc and len(fam) >= 4:
            out.append((fam, orc))
    return out

GITHUB_RESERVED = {
    "about", "apps", "collections", "customer-stories", "explore", "features",
    "issues", "join", "login", "marketplace", "new", "notifications", "orgs",
    "pricing", "pulls", "search", "settings", "sponsors", "topics", "trending",
}

_GITHUB_PROFILE_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})", re.IGNORECASE)

_GITHUB_PAGES_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9-]{0,38})\.github\.io", re.IGNORECASE)

def logins_in(text: str) -> list[str]:
    """Достаёт логины GitHub из произвольного текста."""
    found: list[str] = []
    for pattern in (_GITHUB_PROFILE_RE, _GITHUB_PAGES_RE):
        for match in pattern.finditer(text or ""):
            login = match.group(1).rstrip("-")
            if login and login.lower() not in GITHUB_RESERVED:
                found.append(login)
    seen, unique = set(), []
    for login in found:
        if login.lower() not in seen:
            seen.add(login.lower())
            unique.append(login)
    return unique

def extract_from_person(person: dict) -> tuple[list[dict], list[dict]]:
    """Из ORCID/person: github-логины (findings) + researcher-urls."""
    researcher_urls: list[dict] = []
    sources: list[tuple[str, str]] = []
    for entry in (person.get("researcher-urls") or {}).get("researcher-url", []):
        name = entry.get("url-name")
        url = (entry.get("url") or {}).get("value")
        if url:
            researcher_urls.append({"name": name, "url": url})
            sources.append((url, f"researcher-url:{name}" if name else "researcher-url"))
    for entry in (person.get("external-identifiers") or {}).get("external-identifier", []):
        url = (entry.get("external-id-url") or {}).get("value") or ""
        value = entry.get("external-id-value") or ""
        sources.append((f"{url} {value}", "external-id"))
    for entry in (person.get("keywords") or {}).get("keyword", []):
        if entry.get("content"):
            sources.append((entry["content"], "keyword"))
    bio = (person.get("biography") or {}).get("content")
    if bio:
        sources.append((bio, "biography"))
    findings: dict[str, dict] = {}
    for text, source in sources:
        for login in logins_in(text):
            findings.setdefault(login.lower(), {
                "login": login, "url": f"https://github.com/{login}", "source": source})
    return list(findings.values()), researcher_urls

def parse_openalex_author(a: dict) -> dict:
    """Наукометрия, аффилиации и внешние id из OpenAlex author."""
    ids = a.get("ids") or {}
    stats = a.get("summary_stats") or {}
    affiliations = [
        {
            "name": (aff.get("institution") or {}).get("display_name"),
            "ror": tail_id((aff.get("institution") or {}).get("ror")),
            "country": (aff.get("institution") or {}).get("country_code"),
            "years": aff.get("years") or [],
        }
        for aff in a.get("affiliations") or []
    ]
    last = a.get("last_known_institutions") or []
    topics = [
        {"name": t.get("display_name"), "count": t.get("count"),
         "field": ((t.get("field") or {}).get("display_name"))}
        for t in (a.get("topics") or [])[:TOPICS_LIMIT]
    ]
    return {
        "scopus_id": tail_id(ids.get("scopus")) if ids.get("scopus") else None,
        "twitter": ids.get("twitter"),
        "wikipedia": ids.get("wikipedia"),
        "works_count": a.get("works_count"),
        "cited_by_count": a.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "last_institution": last[0].get("display_name") if last else None,
        "country": last[0].get("country_code") if last else None,
        "affiliations": affiliations,
        "topics": topics,
        "counts_by_year": a.get("counts_by_year") or [],
    }

def _affiliation_rows(group_block: dict, summary_key: str) -> list[dict]:
    """Разбирает блок employments/educations ORCID в список словарей."""
    rows: list[dict] = []
    for group in (group_block or {}).get("affiliation-group", []):
        for summary in group.get("summaries", []):
            e = summary.get(summary_key, {})
            start = (e.get("start-date") or {}).get("year") or {}
            end = (e.get("end-date") or {}).get("year") or {}
            org = e.get("organization") or {}
            addr = org.get("address") or {}
            rows.append({
                "org": org.get("name"), "role": e.get("role-title"),
                "start": start.get("value"), "end": end.get("value"),
                "country": addr.get("country"),
            })
    return rows

def parse_orcid_record(record: dict) -> dict:
    """История работы/учёбы, внешние id, страна, keywords, emails из ORCID."""
    activities = record.get("activities-summary") or {}
    person = record.get("person") or {}
    employments = _affiliation_rows(activities.get("employments"), "employment-summary")
    educations = _affiliation_rows(activities.get("educations"), "education-summary")

    external_ids = []
    scopus_id = researcher_id = linkedin = None
    for x in (person.get("external-identifiers") or {}).get("external-identifier", []):
        ext_type = x.get("external-id-type")
        value = x.get("external-id-value")
        url = (x.get("external-id-url") or {}).get("value")
        external_ids.append({"type": ext_type, "value": value, "url": url})
        low = (ext_type or "").lower()
        if "scopus" in low and not scopus_id:
            scopus_id = value
        elif "researcher" in low and not researcher_id:
            researcher_id = value
        elif "linkedin" in low and not linkedin:
            linkedin = url or value

    _, researcher_urls = extract_from_person(person)
    for ru in researcher_urls:
        if linkedin is None and "linkedin.com" in (ru.get("url") or "").lower():
            linkedin = ru["url"]

    keywords = [k.get("content") for k in (person.get("keywords") or {}).get("keyword", [])
                if k.get("content")]
    bio = (person.get("biography") or {}).get("content")
    country = None
    for addr in (person.get("addresses") or {}).get("address", []):
        country = (addr.get("country") or {}).get("value")
        if country:
            break
    emails = [e.get("email") for e in (person.get("emails") or {}).get("email", [])
              if e.get("email")]

    name = person.get("name") or {}
    other_names = []
    credit = (name.get("credit-name") or {}).get("value")
    if credit:
        other_names.append(credit)
    for o in (person.get("other-names") or {}).get("other-name", []):
        if o.get("content"):
            other_names.append(o["content"])

    return {
        "employments": employments, "educations": educations, "external_ids": external_ids,
        "scopus_id": scopus_id, "researcher_id": researcher_id, "linkedin": linkedin,
        "researcher_urls": researcher_urls, "keywords": keywords, "biography": bio,
        "country": country, "emails": emails, "other_names": other_names,
    }

def _get_json(url, params=None, headers=None, retries=3):
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("запрос упал %s: %s", url, exc)
        return None
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            return None
    if resp.status_code == 429 and retries > 0:
        time.sleep(60)   # запас; с рейт-лимитером 429 редок
        return _get_json(url, params, headers, retries - 1)
    if resp.status_code != 404:
        logger.warning("%d для %s", resp.status_code, url)
    return None

def fetch_openalex_author(author_id: str) -> dict | None:
    params = {"mailto": USER_AGENT_EMAIL}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    return _get_json(f"{OPENALEX_AUTHORS_URL}/{author_id}", params=params,
                     headers={"User-Agent": USER_AGENT})

def fetch_orcid_record(orcid: str) -> dict | None:
    return _get_json(f"{ORCID_PUBLIC_API}/{orcid}/record", headers={"Accept": "application/json"})
