"""Чистые хелперы обработки: парсеры ORCID/OpenAlex/PDF, извлечение ссылок,
скоринг github-матчинга, нормализация и дедуп-структуры. Выделены из старого
store-mediated мира — SQL и классов-операций здесь нет.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlparse

import fitz
import requests

from .config import (
    CONTEXT_RADIUS,
    CROSSREF_TIMEOUT,
    CROSSREF_URL,
    HTTP_TIMEOUT,
    OPENALEX_API_KEY,
    OPENALEX_AUTHORS_URL,
    OPENREVIEW_API_URL,
    OPENREVIEW_PASSWORD,
    OPENREVIEW_RATE_LIMIT_SLEEP,
    OPENREVIEW_USERNAME,
    ORCID_PUBLIC_API,
    TOPICS_LIMIT,
    USER_AGENT,
    USER_AGENT_EMAIL,
)

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


def alpha(s: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())


def orcid_tail(u: str | None) -> str | None:
    return u.rstrip("/").split("/")[-1] if u else None


def author_surnames(name_en: str, variants: str | None) -> set[str]:
    """Фамилии (последний токен) из name_en + name_variants, длиной >= 4."""
    surnames = set()
    try:
        names = [name_en] + (json.loads(variants) if variants else [])
    except (TypeError, ValueError):
        names = [name_en]
    for n in names:
        toks = [alpha(t) for t in (n or "").split() if alpha(t)]
        if len(toks) >= 2 and len(toks[-1]) >= 4:
            surnames.add(toks[-1])
    return surnames


def surname_match(family: str, surnames: set[str]) -> bool:
    return any(s == family or s in family or family in s for s in surnames)


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


URL_PATTERN = re.compile(
    r'(?<![\w.])(?:https?://)?(?:www\.)?(github\.com)(/[^\s<>)\]}"\']+)', re.IGNORECASE
)


HYPHEN_LINEBREAK = re.compile(r"(?<=\w)-\n\s*(?=\w)")


URL_LINEBREAK = re.compile(r"(?<=[\w/.\-?&=#%])\n\s*(?=[\w/])")


SENTENCE_START_TAIL = re.compile(r"\.(?:[A-Z][a-z]+[\w-]*|[A-Z]{2,}[\w-]*|\d+(?:\.\d+)*)$")


URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"


SOURCE_TEXT = "pdf_text"


SOURCE_ANNOT = "pdf_annotation"


SOURCE_ABSTRACT = "abstract"


SOURCE_PRIORITY = {SOURCE_TEXT: 0, SOURCE_ANNOT: 1, SOURCE_ABSTRACT: 2}


def split_at_embedded_url(url: str) -> str:
    """Отрезает второй вложенный http(s):// (склейка ссылки и сноски в PDF)."""
    positions = [p for p in (url.find("http://", 1), url.find("https://", 1)) if p > 0]
    return url[: min(positions)] if positions else url


def canonicalize_repo_url(url: str) -> str:
    """Сворачивает github-URL к https://github.com/user/repo (без /tree, .git и т.п.)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    segments = [seg for seg in parsed.path.split("/") if seg]
    if len(segments) < 2:
        return url
    repo = segments[1]
    if ".git" in repo:  # "repo.gitReceived" → "repo"
        repo = repo.split(".git", 1)[0]
    return f"https://github.com/{segments[0]}/{repo}"


def clean_url_tail(url: str) -> str:
    url = SENTENCE_START_TAIL.sub("", url.rstrip(URL_TRAILING_PUNCT))
    url = split_at_embedded_url(url)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return canonicalize_repo_url(url)


def normalize_pdf_text(text: str) -> str:
    return URL_LINEBREAK.sub("", HYPHEN_LINEBREAK.sub("", text))


def is_github(url: str) -> bool:
    try:
        netloc = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return False
    return netloc == "github.com" or netloc.endswith(".github.com")


def find_urls_in_text(text: str) -> list[tuple[str, int, int]]:
    return [(clean_url_tail(m.group(0)), m.start(), m.end()) for m in URL_PATTERN.finditer(text)]


def slice_context(text: str, start: int, end: int) -> str:
    return text[max(0, start - CONTEXT_RADIUS): min(len(text), end + CONTEXT_RADIUS)]


def extract_from_pdf_page(page) -> list[tuple[str, str, str]]:
    """(url, context, source) с одной страницы: видимый текст + гиперссылки."""
    results = []
    raw_text = page.get_text()
    normalized = normalize_pdf_text(raw_text) if raw_text else ""
    for url, start, end in find_urls_in_text(normalized):
        results.append((url, slice_context(normalized, start, end), SOURCE_TEXT))
    for link in page.get_links() or []:
        uri = link.get("uri")
        if not uri or link.get("kind") != fitz.LINK_URI or not is_github(uri):
            continue
        url = clean_url_tail(uri)
        rect = link.get("from")
        visible = ""
        if rect is not None:
            try:
                visible = page.get_text("text", clip=rect).strip()
            except Exception:
                visible = ""
        context = visible or url
        if visible and normalized:
            idx = normalized.find(visible)
            if idx >= 0:
                context = slice_context(normalized, idx, idx + len(visible))
        results.append((url, context, SOURCE_ANNOT))
    return results


def extract_from_pdf(pdf_path: str) -> list[tuple[str, str, int | None, str]]:
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logger.warning("не удалось открыть PDF: %s", exc)
        return []
    results = []
    for page_num, page in enumerate(doc, 1):
        for url, context, source in extract_from_pdf_page(page):
            results.append((url, context, page_num, source))
    doc.close()
    return results


def extract_from_abstract(abstract: str) -> list[tuple[str, str, int | None, str]]:
    if not abstract:
        return []
    normalized = normalize_pdf_text(abstract)
    return [
        (url, slice_context(normalized, start, end), None, SOURCE_ABSTRACT)
        for url, start, end in find_urls_in_text(normalized)
    ]


def deduplicate(links: list[tuple[str, str, int | None, str]]) -> list[tuple[str, str, int | None]]:
    """Одна запись на URL. Приоритет источника: текст PDF > аннотация > абстракт.

    Схлопывание идёт по каноническому URL, поэтому /tree/main, .git и склеенные
    хвосты не дают дублей репозиториев.
    """
    chosen: dict[str, tuple[str, str, int | None, str]] = {}
    for entry in links:
        url = entry[0]
        if url not in chosen or SOURCE_PRIORITY[entry[3]] < SOURCE_PRIORITY[chosen[url][3]]:
            chosen[url] = entry
    return [(url, context, page) for url, context, page, _ in chosen.values()]


CLASSIFY_PROMPT = """Ты помогаешь анализировать научные публикации.

Публикация:
  Название: {title}
  Авторы:   {authors}

В её материалах найдена ссылка:
  URL:  {url}
  {source_hint}

Окружающий текст:
\"\"\"
{context}
\"\"\"

Вопрос: это репозиторий/модель/датасет, который ВЫЛОЖИЛИ САМИ АВТОРЫ этой
статьи как сопроводительный материал — или это упоминание чужого инструмента?

Признаки авторского: "our code is available at", "we release", "наш код доступен";
имя пользователя/организации в URL похоже на автора или его аффилиацию.
Признаки чужого: ссылка в списке литературы; известная чужая библиотека
(PyTorch, BERT, Llama); формулировки "we use", "based on", "following [N]".

Ответь СТРОГО валидным JSON без markdown:
{{"is_authors_artifact": true, "confidence": 0.0, "reason": "одно короткое предложение"}}
"""


def parse_variants(raw: str | None) -> list[str]:
    """name_variants хранится как JSON или python-list-литерал — парсим оба."""
    if not raw:
        return []
    for loader in (json.loads, ast.literal_eval):
        try:
            v = loader(raw)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        except (ValueError, SyntaxError, TypeError):
            continue
    return []


GITHUB_RESERVED = {
    "about", "apps", "collections", "customer-stories", "explore", "features",
    "issues", "join", "login", "marketplace", "new", "notifications", "orgs",
    "pricing", "pulls", "search", "settings", "sponsors", "topics", "trending",
}


_GITHUB_PROFILE_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})", re.IGNORECASE)


_GITHUB_PAGES_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9-]{0,38})\.github\.io", re.IGNORECASE)


def tail_id(url: str | None) -> str | None:
    """Последний сегмент URL - для orcid/openalex/ror id."""
    return url.rstrip("/").split("/")[-1] if url else None


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


LINK_KEYS = ("homepage", "gscholar", "dblp", "orcid", "github", "linkedin")


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"[^a-zа-я0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def tail(v: str | None) -> str | None:
    return v.rstrip("/").split("/")[-1] if v else None


def jloads(s):
    try:
        return json.loads(s) if s else []
    except (TypeError, ValueError):
        return []


def search_terms(name_en: str):
    """Имя целиком, затем «Имя Фамилия» без инициалов."""
    yield name_en
    toks = [t for t in name_en.split() if len(t.strip(".")) > 1]
    if len(toks) >= 2 and f"{toks[0]} {toks[-1]}" != name_en:
        yield f"{toks[0]} {toks[-1]}"


class OpenReviewClient:
    """Логин в OpenReview один раз + поиск профилей по имени."""

    def __init__(self) -> None:
        self.s = requests.Session()
        r = self.s.post(
            f"{OPENREVIEW_API_URL}/login",
            json={"id": OPENREVIEW_USERNAME, "password": OPENREVIEW_PASSWORD},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['token']}"

    def search(self, term: str) -> list[dict]:
        try:
            r = self.s.get(
                f"{OPENREVIEW_API_URL}/profiles/search", params={"term": term},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return []
        if r.status_code == 429:
            time.sleep(OPENREVIEW_RATE_LIMIT_SLEEP)
            return self.search(term)
        if r.status_code != 200:
            return []
        return r.json().get("profiles", [])


def verify(content: dict, name_norms: set[str], our_orcid: str | None) -> str | None:
    """Возвращает способ подтверждения принадлежности ИТМО или None."""
    cand_orcid = tail(content.get("orcid"))
    if our_orcid and cand_orcid and our_orcid == cand_orcid:
        return "orcid"
    cand_names = {norm(n.get("fullname")) for n in content.get("names", []) if n.get("fullname")}
    if not (cand_names & name_norms):
        return None
    emails = content.get("emails") or []
    if any((e or "").lower().endswith("@itmo.ru") for e in emails):
        return "itmo_email"
    if any("itmo" in ((h.get("institution") or {}).get("name") or "").lower()
           for h in content.get("history", [])):
        return "itmo_affil"
    return None


def extract(profile: dict, matched_by: str, name_en: str) -> dict:
    c = profile.get("content", {})
    links = {k: c.get(k) for k in LINK_KEYS}
    return {
        "openreview_id": profile.get("id"),
        "name_en": name_en,
        "matched_by": matched_by,
        "names": [n.get("fullname") for n in c.get("names", []) if n.get("fullname")],
        "emails_masked": c.get("emails") or [],
        "affiliations": [
            {"name": (h.get("institution") or {}).get("name"),
             "position": h.get("position"), "start": h.get("start"), "end": h.get("end")}
            for h in c.get("history", [])
        ],
        "relations": [
            {"relation": r.get("relation"), "name": r.get("name")}
            for r in c.get("relations", [])
        ],
        **links,
    }


TLD = (r"(?:ru|com|org|net|edu|gov|io|info|biz|name|eu|de|fr|uk|us|cn|jp|kr"
       r"|in|it|es|nl|se|fi|no|ch|at|cz|pl|by|kz|ua)")


EMAIL_RE = re.compile(rf"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+?\.{TLD}(?![A-Za-z])", re.IGNORECASE)


BRACE_RE = re.compile(rf"\{{([^{{}}@]+)\}}@([A-Za-z0-9.\-]+?\.{TLD})(?![A-Za-z])", re.IGNORECASE)  # {a,b}@domain


def match_authors(local_part: str, authors: list[tuple[str, set[str]]]) -> list[str]:
    """person_id, чья фамилия встречается в локальной части адреса."""
    lp = alpha(local_part)
    if not lp:
        return []
    return [pid for pid, surnames in authors if any(s in lp for s in surnames)]


def emails_from_pdf_text(text: str) -> set[str]:
    found = set()
    for inside, dom in BRACE_RE.findall(text):
        for part in re.split(r"[;,]", inside):
            part = part.strip().strip(".")
            if part:
                found.add(f"{part}@{dom}".lower())
    for m in EMAIL_RE.findall(text):
        found.add(m.lower().rstrip("."))
    return found


def emails_from_pdf(path) -> set[str]:
    try:
        doc = fitz.open(path)
    except Exception:
        return set()
    out: set[str] = set()
    for page in doc:
        out |= emails_from_pdf_text(page.get_text() or "")
    doc.close()
    return out


NAME_EXACT = 0.999


NAME_FUZZY = 0.86


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_sim(a: str, b: str) -> float:
    """Сходство двух нормализованных имён: равенство множеств токенов или ratio."""
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    if len(ta) >= 2 and ta == tb:
        return 1.0

    return SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()


def best_name_sim(cands: set[str], persons: set[str]) -> tuple[float, tuple[str, str]]:
    best, pair = 0.0, ("", "")
    for c in cands:
        for p in persons:
            s = name_sim(c, p)
            if s > best:
                best, pair = s, (c, p)
    return best, pair


def login_has_surname(login: str, surname: str | None) -> bool:
    """Логин содержит фамилию персоны"""
    if not surname:
        return False
    low = login.lower()
    return surname in low or SequenceMatcher(None, low, surname).ratio() >= 0.85


def norm_email(e: str | None) -> str:
    return (e or "").strip().lower()


def repo_owner(url: str | None) -> str | None:
    m = re.search(r"github\.com/([^/]+)/", url or "")
    return m.group(1).lower() if m else None


def score_person(cand: dict, person: dict, email_hit: bool):
    """Возвращает (score, signals, evidence) для пары логин<->персона."""
    signals: list[str] = []
    evidence: dict = {}

    if email_hit:
        signals.append("email_exact")
        evidence["email"] = sorted(cand["emails"] & person["emails"])

    sim, pair = best_name_sim(cand["names"], person["names"])
    if sim >= NAME_EXACT:
        signals.append("name_exact")
    elif sim >= NAME_FUZZY:
        signals.append("name_fuzzy")
    if sim >= NAME_FUZZY:
        evidence["name_pair"], evidence["name_sim"] = pair, round(sim, 2)

    itmo_email = any(e.endswith("@itmo.ru") for e in cand["emails"])
    if cand["itmo_text"]:
        signals.append("itmo_profile")
    if itmo_email:
        signals.append("itmo_email")

    if login_has_surname(cand["login"], person.get("surname")):
        signals.append("login_surname")
    if cand["is_owner"]:
        signals.append("owner")
    if cand["org_itmo"]:
        signals.append("org_itmo")

    weights = {"email_exact": 1.0, "name_exact": 0.6, "name_fuzzy": 0.4,
               "itmo_profile": 0.2, "itmo_email": 0.3, "login_surname": 0.3,
               "owner": 0.3, "org_itmo": 0.3}
    score = min(1.0, sum(weights[s] for s in signals))
    return score, signals, evidence


def decide(signals: list[str], in_bridge: bool) -> str:
    if "email_exact" in signals:
        return "matched"
    corrob = any(s in signals for s in
                 ("itmo_profile", "itmo_email", "login_surname", "owner", "org_itmo"))
    if "name_exact" in signals:
        if in_bridge:
            return "matched"
        return "matched" if corrob else "review"
    if "name_fuzzy" in signals:
        if in_bridge and corrob:
            return "matched"
        if in_bridge:
            return "review"
        return "rejected"
    return "rejected"


def match_login(login, cand, persons, email_index, name_index, bridge):
    """Лучшая персона для одного логина: (person_id, score, signals, evidence)."""
    bridge_pids = set()
    for pub in cand["pub_ids"]:
        bridge_pids |= bridge.get(pub, set())

    email_pids = {email_index[e] for e in cand["emails"] if e in email_index}

    global_pids = set()
    for n in cand["names"]:
        if len(n.split()) >= 2:
            global_pids |= name_index.get(n, set())

    best = None
    matched = []
    for pid in bridge_pids | email_pids | global_pids:
        if pid not in persons:
            continue
        in_bridge = pid in bridge_pids
        score, signals, evidence = score_person(cand, persons[pid], pid in email_pids)
        d = decide(signals, in_bridge)
        if d == "rejected":
            continue
        evidence["in_bridge"] = in_bridge
        rank = (d == "matched", score)
        if best is None or rank > best[0]:
            best = (rank, pid, score, signals, evidence, d)
        if d == "matched":
            matched.append((score, pid))
    if best is None:
        return None
    _, pid, score, signals, evidence, d = best
    if d == "matched":
        top = max(s for s, _ in matched)
        if len({p for s, p in matched if s == top}) > 1:
            d = "review"
            evidence["ambiguous"] = True
    return pid, score, signals, evidence, d


def usable_email(e: str | None) -> str | None:
    e = (e or "").strip().lower()
    return e if e and "@" in e and "noreply" not in e else None


def pick_email(cands: list[tuple[str, str]]) -> str | None:
    """Из кандидатов (src, email) — институциональный, иначе по порядку источника."""
    inst = [e for _, e in cands if e.endswith(("@itmo.ru", "ifmo.ru"))]
    if inst:
        return inst[0]
    order = {"orcid": 0, "page": 1, "pdf": 2, "commit": 3}
    return min(cands, key=lambda x: order.get(x[0], 9))[1] if cands else None


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self.parent:
            out.setdefault(self.find(node), []).append(node)
        return out


def normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'["“”‘’«»]', "", s)
    return re.sub(r"\s+", " ", s).strip().lower()



MAILTO_RE = re.compile(r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.I)

SKIP_HOSTS = ("github.com", "linkedin.com", "scholar.google", "researchgate",
              "orcid.org", "twitter.com", "x.com", "facebook", "youtube",
              "t.me", "vk.com", "semanticscholar", "dblp.org", "publons")

def is_page(u: str | None) -> bool:
    u = (u or "").lower()
    return u.startswith("http") and not any(s in u for s in SKIP_HOSTS)

def deobfuscate(t: str) -> str:
    t = re.sub(r"&#0*64;|&#x0*40;|&commat;", "@", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", "@", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", ".", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*@\s*", "@", t)
    return t

def emails_from_html(html: str) -> set[str]:
    found = {m.lower() for m in MAILTO_RE.findall(html)}
    found |= {m.lower() for m in EMAIL_RE.findall(deobfuscate(html))}
    return found
