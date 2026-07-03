import argparse
import json
import re
import sqlite3
import time

import requests
from config import (
    DB_PATH,
    HTTP_TIMEOUT,
    OPENALEX_API_KEY,
    OPENALEX_AUTHORS_URL,
    ORCID_PUBLIC_API,
    ORCID_REQUEST_DELAY,
    RATE_LIMIT_SLEEP,
    REQUEST_DELAY,
    SQLITE_TIMEOUT,
    USER_AGENT,
    USER_AGENT_EMAIL,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS person_profiles (
    person_id          TEXT PRIMARY KEY,
    name_en            TEXT,
    openalex_author_id TEXT,
    openalex_url       TEXT,
    orcid              TEXT,
    scopus_id          TEXT,
    researcher_id      TEXT,   -- Web of Science ResearcherID (ORCID)
    twitter            TEXT,   -- из OpenAlex ids, если привязан к ORCID
    wikipedia          TEXT,
    linkedin           TEXT,
    country            TEXT,
    works_count        INTEGER,
    cited_by_count     INTEGER,
    h_index            INTEGER,
    i10_index          INTEGER,
    last_institution   TEXT,   -- последний известный вуз (display_name)
    affiliations       TEXT,   -- JSON [{name, ror, country, years}]  (OpenAlex)
    employments        TEXT,   -- JSON [{org, role, start, end, country}] (ORCID)
    educations         TEXT,   -- JSON [{org, role, start, end, country}] (ORCID)
    topics             TEXT,   -- JSON [{name, count, field}]  (OpenAlex)
    counts_by_year     TEXT,   -- JSON [{year, works_count, cited_by_count}]
    researcher_urls    TEXT,   -- JSON [{name, url}]  (ORCID)
    external_ids       TEXT,   -- JSON [{type, value, url}]  (ORCID)
    keywords           TEXT,   -- JSON [str]  (ORCID)
    biography          TEXT,
    emails             TEXT,   -- JSON [str]  публичные email 
    other_names        TEXT,   -- JSON [str]  credit-name + other-names (ORCID)
    has_github         INTEGER DEFAULT 0,
    github_urls        TEXT,   -- JSON [str],
    status             TEXT,   -- enriched | no_orcid | orcid_error
    enriched_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_profiles_status   ON person_profiles(status);
CREATE INDEX IF NOT EXISTS idx_profiles_github   ON person_profiles(has_github);
"""

TOPICS_LIMIT = 8


def _tail_id(url: str | None) -> str | None:
    """Последний сегмент URL - для orcid/openalex/ror id."""
    return url.rstrip("/").split("/")[-1] if url else None


GITHUB_RESERVED = {
    "about", "apps", "collections", "customer-stories", "explore", "features",
    "issues", "join", "login", "marketplace", "new", "notifications", "orgs",
    "pricing", "pulls", "search", "settings", "sponsors", "topics", "trending",
}
_GITHUB_PROFILE_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})", re.IGNORECASE)
_GITHUB_PAGES_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9-]{0,38})\.github\.io", re.IGNORECASE)


def _logins_in(text: str) -> list[str]:
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
        for login in _logins_in(text):
            findings.setdefault(login.lower(), {
                "login": login, "url": f"https://github.com/{login}", "source": source})
    return list(findings.values()), researcher_urls


def parse_openalex_author(a: dict) -> dict:
    """Вытаскивает наукометрию, аффилиации и внешние id из OpenAlex author."""
    ids = a.get("ids") or {}
    stats = a.get("summary_stats") or {}

    affiliations = []
    for aff in a.get("affiliations") or []:
        inst = aff.get("institution") or {}
        affiliations.append(
            {
                "name": inst.get("display_name"),
                "ror": _tail_id(inst.get("ror")),
                "country": inst.get("country_code"),
                "years": aff.get("years") or [],
            }
        )

    last = a.get("last_known_institutions") or []
    topics = [
        {
            "name": t.get("display_name"),
            "count": t.get("count"),
            "field": ((t.get("field") or {}).get("display_name")),
        }
        for t in (a.get("topics") or [])[:TOPICS_LIMIT]
    ]

    return {
        "scopus_id": _tail_id(ids.get("scopus")) if ids.get("scopus") else None,
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
            rows.append(
                {
                    "org": org.get("name"),
                    "role": e.get("role-title"),
                    "start": start.get("value"),
                    "end": end.get("value"),
                    "country": addr.get("country"),
                }
            )
    return rows


def parse_orcid_record(record: dict) -> dict:
    """Вытаскивает историю работы/учёбы, внешние id, страну, keywords из ORCID."""
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

    keywords = [
        k.get("content")
        for k in (person.get("keywords") or {}).get("keyword", [])
        if k.get("content")
    ]
    bio = (person.get("biography") or {}).get("content")
    country = None
    for addr in (person.get("addresses") or {}).get("address", []):
        country = (addr.get("country") or {}).get("value")
        if country:
            break

    emails = [
        e.get("email")
        for e in (person.get("emails") or {}).get("email", [])
        if e.get("email")
    ]

    name = person.get("name") or {}
    other_names = []
    credit = (name.get("credit-name") or {}).get("value")
    if credit:
        other_names.append(credit)
    for o in (person.get("other-names") or {}).get("other-name", []):
        if o.get("content"):
            other_names.append(o["content"])

    return {
        "employments": employments,
        "educations": educations,
        "external_ids": external_ids,
        "scopus_id": scopus_id,
        "researcher_id": researcher_id,
        "linkedin": linkedin,
        "researcher_urls": researcher_urls,
        "keywords": keywords,
        "biography": bio,
        "country": country,
        "emails": emails,
        "other_names": other_names,
    }


class PersonEnricher:
    """Обогащает сотрудников ИТМО полными профилями из OpenAlex и ORCID"""

    def __init__(self, source_db, out_db, limit, refresh) -> None:
        self.source_db = source_db
        self.out_db = out_db
        self.limit = limit
        self.refresh = refresh

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        self.stats = {"processed": 0, "enriched": 0, "no_orcid": 0,
                      "orcid_error": 0, "github_found": 0}
        self.api_stats = {"openalex": 0, "orcid": 0}

    # --- HTTP ------------------------------------------------------------

    def _get_json(self, url, params=None, headers=None, retries=3):
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            print(f"  запрос упал {url}: {exc}")
            return None
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return None
        if resp.status_code == 429 and retries > 0:
            print(f"  429 для {url}, sleep {RATE_LIMIT_SLEEP} сек (осталось попыток: {retries - 1})")
            time.sleep(RATE_LIMIT_SLEEP)
            return self._get_json(url, params, headers, retries - 1)
        if resp.status_code != 404:
            print(f"  {resp.status_code} для {url}")
        return None

    def fetch_author(self, author_id: str) -> dict | None:
        params = {"mailto": USER_AGENT_EMAIL}
        if OPENALEX_API_KEY:
            params["api_key"] = OPENALEX_API_KEY
        self.api_stats["openalex"] += 1
        return self._get_json(f"{OPENALEX_AUTHORS_URL}/{author_id}", params=params)

    def fetch_record(self, orcid: str) -> dict | None:
        self.api_stats["orcid"] += 1
        return self._get_json(
            f"{ORCID_PUBLIC_API}/{orcid}/record", headers={"Accept": "application/json"}
        )

    # --- БД --------------------------------------------------------------

    def _load_people(self, conn, done) -> list[tuple]:
        rows = conn.execute("SELECT id, name_en FROM persons_itmo ORDER BY id").fetchall()
        people = []
        for person_id, name_en in rows:
            if not self.refresh and person_id in done:
                continue
            author_id = person_id[len("itmo_"):] if person_id.startswith("itmo_") else person_id
            if not author_id.startswith("A"):
                continue
            people.append((person_id, name_en, author_id))
            if self.limit and len(people) >= self.limit:
                break
        return people

    @staticmethod
    def _j(value):
        """Сериализует непустую структуру в JSON, иначе None."""
        return json.dumps(value, ensure_ascii=False) if value else None

    def _save(self, out, person_id, name_en, author, oa, orc, github, status) -> None:
        out.execute(
            """
            INSERT OR REPLACE INTO person_profiles (
                person_id, name_en, openalex_author_id, openalex_url, orcid,
                scopus_id, researcher_id, twitter, wikipedia, linkedin, country,
                works_count, cited_by_count, h_index, i10_index, last_institution,
                affiliations, employments, educations, topics, counts_by_year,
                researcher_urls, external_ids, keywords, biography,
                emails, other_names,
                has_github, github_urls, status, enriched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """,
            (
                person_id, name_en,
                author, f"https://openalex.org/{author}",
                (orc or {}).get("orcid"),
                (orc or {}).get("scopus_id") or (oa or {}).get("scopus_id"),
                (orc or {}).get("researcher_id"),
                (oa or {}).get("twitter"), (oa or {}).get("wikipedia"),
                (orc or {}).get("linkedin"),
                (orc or {}).get("country") or (oa or {}).get("country"),
                (oa or {}).get("works_count"), (oa or {}).get("cited_by_count"),
                (oa or {}).get("h_index"), (oa or {}).get("i10_index"),
                (oa or {}).get("last_institution"),
                self._j((oa or {}).get("affiliations")),
                self._j((orc or {}).get("employments")),
                self._j((orc or {}).get("educations")),
                self._j((oa or {}).get("topics")),
                self._j((oa or {}).get("counts_by_year")),
                self._j((orc or {}).get("researcher_urls")),
                self._j((orc or {}).get("external_ids")),
                self._j((orc or {}).get("keywords")),
                (orc or {}).get("biography"),
                self._j((orc or {}).get("emails")),
                self._j((orc or {}).get("other_names")),
                1 if github else 0,
                self._j([g["url"] for g in github]),
                status,
            ),
        )

    # --- Драйвер ---------------------------------------------------------

    def run(self) -> None:
        out = sqlite3.connect(self.out_db, timeout=SQLITE_TIMEOUT)
        out.executescript(SCHEMA_SQL)
        for col in ("emails", "other_names"):
            try:
                out.execute(f"ALTER TABLE person_profiles ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        self.crossref = {}
        try:
            self.crossref = {pid: orc for pid, orc in out.execute(
                "SELECT person_id, orcid FROM crossref_orcid")}
        except sqlite3.OperationalError:
            pass
        reprocess = {pid for (pid,) in out.execute(
            "SELECT person_id FROM person_profiles WHERE status = 'no_orcid'")
        } & set(self.crossref)
        done = {r[0] for r in out.execute(
            "SELECT person_id FROM person_profiles")} - reprocess

        src = sqlite3.connect(self.source_db, timeout=SQLITE_TIMEOUT)
        people = self._load_people(src, done)
        src.close()

        print(f"Источник:  {self.source_db}")
        print(f"Результат: {self.out_db}")
        print(f"К обработке: {len(people)} | уже собрано: {len(done)}\n")

        try:
            for i, (person_id, name_en, author_id) in enumerate(people, 1):
                self.stats["processed"] += 1
                author = self.fetch_author(author_id)
                time.sleep(REQUEST_DELAY)
                oa = parse_openalex_author(author) if author else {}

                orcid = _tail_id((author or {}).get("orcid")) or self.crossref.get(person_id)
                orc, github, status = {}, [], "no_orcid"
                if orcid:
                    record = self.fetch_record(orcid)
                    time.sleep(ORCID_REQUEST_DELAY)
                    if record is None:
                        status = "orcid_error"
                        self.stats["orcid_error"] += 1
                    else:
                        orc = parse_orcid_record(record)
                        orc["orcid"] = orcid
                        github, _ = extract_from_person(record.get("person") or {})
                        status = "enriched"
                        self.stats["enriched"] += 1
                else:
                    self.stats["no_orcid"] += 1

                if github:
                    self.stats["github_found"] += 1

                self._save(out, person_id, name_en, author_id, oa, orc, github, status)

                badge = f">>> {', '.join(g['url'] for g in github)}" if github else status
                h = (oa or {}).get("h_index")
                print(f"  [{i}/{len(people)}] {name_en[:30]:30}  h={h if h is not None else '-':<3}  {badge}")

                if self.stats["processed"] % 20 == 0:
                    out.commit()
            out.commit()
        except KeyboardInterrupt:
            print("\nПрервано пользователем")
            out.commit()
        finally:
            self.print_summary()
            out.close()

    def print_summary(self) -> None:
        print()
        print("Итог обогащения профилей")
        print("-" * 40)
        print(f"  Обработано:             {self.stats['processed']}")
        print(f"  Обогащено с ORCID:    {self.stats['enriched']}")
        print(f"  Без ORCID, только OA:  {self.stats['no_orcid']}")
        print(f"  Ошибок ORCID API:       {self.stats['orcid_error']}")
        print(f"  Из них с GitHub:        {self.stats['github_found']}")
        print()
        print(f"  Запросов к OpenAlex:    {self.api_stats['openalex']}")
        print(f"  Запросов к ORCID:       {self.api_stats['orcid']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собирает полные профили сотрудников ИТМО из OpenAlex + ORCID."
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Сколько сотрудников обработать за запуск.")
    parser.add_argument("--refresh", action="store_true",
                        help="Пересобрать всех заново, а не только новых.")
    parser.add_argument("--source-db", default=str(DB_PATH),
                        help="Путь к исходной БД с persons_itmo.")
    parser.add_argument("--out-db", default=str(DB_PATH),
                        help="Путь к БД для результатов.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    PersonEnricher(
        source_db=args.source_db,
        out_db=args.out_db,
        limit=args.limit,
        refresh=args.refresh,
    ).run()


if __name__ == "__main__":
    main()
