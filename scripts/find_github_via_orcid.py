import argparse
import json
import re
import sqlite3
import time

import requests
from config import (
    DB_PATH,
    OPENALEX_API_KEY,
    OPENALEX_AUTHORS_URL,
    ORCID_PUBLIC_API,
    ORCID_REQUEST_DELAY,
    PROFILES_DB_PATH,
    REQUEST_DELAY,
    USER_AGENT,
    USER_AGENT_EMAIL,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orcid_lookup (
    person_id          TEXT PRIMARY KEY,
    name_en            TEXT,
    openalex_author_id TEXT,
    orcid              TEXT,
    orcid_url          TEXT,
    status             TEXT,   -- github_found | orcid_no_github | no_orcid | error
    researcher_urls    TEXT,   -- JSON-массив [{"name": ..., "url": ...}]
    checked_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS github_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    TEXT NOT NULL,
    name_en      TEXT,
    orcid        TEXT,
    github_login TEXT,
    github_url   TEXT,
    source       TEXT,         -- откуда взяли: researcher-url:<имя> | biography | keyword | external-id
    found_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, github_url)
);

CREATE INDEX IF NOT EXISTS idx_orcid_lookup_status ON orcid_lookup(status);
CREATE INDEX IF NOT EXISTS idx_github_findings_pid ON github_findings(person_id);
"""

GITHUB_RESERVED = {
    "about", "apps", "collections", "customer-stories", "explore", "features",
    "issues", "join", "login", "marketplace", "new", "notifications", "orgs",
    "pricing", "pulls", "search", "settings", "sponsors", "topics", "trending",
}

_GITHUB_PROFILE_RE = re.compile(
    r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})", re.IGNORECASE
)

_GITHUB_PAGES_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9-]{0,38})\.github\.io", re.IGNORECASE
)


def _logins_in(text: str) -> list[str]:
    """Достаёт логины GitHub из произвольного текста."""
    found: list[str] = []
    for pattern in (_GITHUB_PROFILE_RE, _GITHUB_PAGES_RE):
        for match in pattern.finditer(text or ""):
            login = match.group(1).rstrip("-")
            if login and login.lower() not in GITHUB_RESERVED:
                found.append(login)
    seen: set[str] = set()
    unique: list[str] = []
    for login in found:
        key = login.lower()
        if key not in seen:
            seen.add(key)
            unique.append(login)
    return unique


def extract_from_person(person: dict) -> tuple[list[dict], list[dict]]:
    """Разбирает ответ ORCID/person"""
    researcher_urls: list[dict] = []

    sources: list[tuple[str, str]] = []

    for entry in (person.get("researcher-urls") or {}).get("researcher-url", []):
        name = entry.get("url-name")
        url = (entry.get("url") or {}).get("value")
        if url:
            researcher_urls.append({"name": name, "url": url})
            tag = f"researcher-url:{name}" if name else "researcher-url"
            sources.append((url, tag))

    for entry in (person.get("external-identifiers") or {}).get(
        "external-identifier", []
    ):
        url = (entry.get("external-id-url") or {}).get("value") or ""
        value = entry.get("external-id-value") or ""
        sources.append((f"{url} {value}", "external-id"))

    for entry in (person.get("keywords") or {}).get("keyword", []):
        content = entry.get("content")
        if content:
            sources.append((content, "keyword"))

    bio = (person.get("biography") or {}).get("content")
    if bio:
        sources.append((bio, "biography"))

    findings: dict[str, dict] = {}
    for text, source in sources:
        for login in _logins_in(text):
            key = login.lower()
            if key not in findings:
                findings[key] = {
                    "login": login,
                    "url": f"https://github.com/{login}",
                    "source": source,
                }
    return list(findings.values()), researcher_urls


class OrcidGithubFinder:
    """Прогоняет сотрудников ИТМО по цепочке OpenAlex -> ORCID -> GitHub"""

    def __init__(self, source_db, out_db, limit: int | None, refresh: bool) -> None:
        self.source_db = source_db
        self.out_db = out_db
        self.limit = limit
        self.refresh = refresh

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        self.stats = {
            "processed": 0,
            "github_found": 0,
            "orcid_no_github": 0,
            "no_orcid": 0,
            "error": 0,
            "links_total": 0,
        }
        self.api_stats = {"openalex_requests": 0, "orcid_requests": 0}

    # --- HTTP ------------------------------------------------------------

    def _get_json(self, url: str, params=None, headers=None, retries: int = 3):
        """GET с разбором JSON и обработкой 429"""
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            print(f"  запрос упал {url}: {exc}")
            return None
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return None
        if resp.status_code == 429 and retries > 0:
            print(f"  429 для {url}, sleep 60 сек (осталось попыток: {retries - 1})")
            time.sleep(60)
            return self._get_json(url, params, headers, retries - 1)
        if resp.status_code != 404:
            print(f"  {resp.status_code} для {url}")
        return None

    def fetch_orcid(self, author_id: str) -> tuple[str | None, str | None]:
        """OpenAlex author id -> (ORCID, полный ORCID-URL) либо (None, None)."""
        params = {"mailto": USER_AGENT_EMAIL}
        if OPENALEX_API_KEY:
            params["api_key"] = OPENALEX_API_KEY
        data = self._get_json(f"{OPENALEX_AUTHORS_URL}/{author_id}", params=params)
        self.api_stats["openalex_requests"] += 1
        orcid_url = (data or {}).get("orcid")
        if not orcid_url:
            return None, None
        return orcid_url.rstrip("/").split("/")[-1], orcid_url

    def fetch_person(self, orcid: str) -> dict | None:
        """Публичная карточка /person из ORCID."""
        data = self._get_json(
            f"{ORCID_PUBLIC_API}/{orcid}/person", headers={"Accept": "application/json"}
        )
        self.api_stats["orcid_requests"] += 1
        return data

    # --- БД --------------------------------------------------------------

    def _load_people(self, conn: sqlite3.Connection, done: set[str]) -> list[tuple]:
        """Сотрудники ИТМО с валидным OpenAlex author id, ещё не обработанные."""
        rows = conn.execute(
            "SELECT id, name_en FROM persons_itmo ORDER BY id"
        ).fetchall()
        people: list[tuple] = []
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

    def _save(self, out: sqlite3.Connection, person_id, name_en, author_id,
              orcid, orcid_url, status, researcher_urls, findings) -> None:
        out.execute(
            """
            INSERT OR REPLACE INTO orcid_lookup
                (person_id, name_en, openalex_author_id, orcid, orcid_url,
                 status, researcher_urls, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (person_id, name_en, author_id, orcid, orcid_url, status,
             json.dumps(researcher_urls, ensure_ascii=False) if researcher_urls else None),
        )
        out.execute("DELETE FROM github_findings WHERE person_id = ?", (person_id,))
        for f in findings:
            out.execute(
                """
                INSERT OR IGNORE INTO github_findings
                    (person_id, name_en, orcid, github_login, github_url, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (person_id, name_en, orcid, f["login"], f["url"], f["source"]),
            )

    # --- Драйвер ---------------------------------------------------------

    def run(self) -> None:
        out = sqlite3.connect(self.out_db, timeout=30)
        out.executescript(SCHEMA_SQL)
        done = {r[0] for r in out.execute("SELECT person_id FROM orcid_lookup")}

        src = sqlite3.connect(self.source_db, timeout=30)
        people = self._load_people(src, done)
        src.close()

        print(f"Источник:  {self.source_db}")
        print(f"Результат: {self.out_db}")
        print(f"К обработке сотрудников: {len(people)} "
              f"(уже было в базе: {len(done)})\n")

        try:
            for i, (person_id, name_en, author_id) in enumerate(people, 1):
                self.stats["processed"] += 1
                orcid, orcid_url = self.fetch_orcid(author_id)
                time.sleep(REQUEST_DELAY)

                if not orcid:
                    self.stats["no_orcid"] += 1
                    self._save(out, person_id, name_en, author_id, None, None,
                               "no_orcid", [], [])
                    print(f"  [{i}/{len(people)}] {name_en[:32]:32}  нет ORCID")
                    continue

                person = self.fetch_person(orcid)
                time.sleep(ORCID_REQUEST_DELAY)
                if person is None:
                    self.stats["error"] += 1
                    self._save(out, person_id, name_en, author_id, orcid, orcid_url,
                               "error", [], [])
                    print(f"  [{i}/{len(people)}] {name_en[:32]:32}  {orcid}  ORCID недоступен")
                    continue

                findings, researcher_urls = extract_from_person(person)
                if findings:
                    status = "github_found"
                    self.stats["github_found"] += 1
                    self.stats["links_total"] += len(findings)
                    urls = ", ".join(f["url"] for f in findings)
                    print(f"  [{i}/{len(people)}] {name_en[:32]:32}  {orcid}  >>> {urls}")
                else:
                    status = "orcid_no_github"
                    self.stats["orcid_no_github"] += 1
                    print(f"  [{i}/{len(people)}] {name_en[:32]:32}  {orcid}  ORCID есть, GitHub нет")

                self._save(out, person_id, name_en, author_id, orcid, orcid_url,
                           status, researcher_urls, findings)

                if self.stats["processed"] % 20 == 0:
                    out.commit()
            out.commit()
        except KeyboardInterrupt:
            print("\nПрервано пользователем.")
            out.commit()
        finally:
            self.print_summary()
            out.close()

    def print_summary(self) -> None:
        print()
        print("Итог поиска GitHub через ORCID")
        print("-" * 40)
        print(f"  Сотрудников обработано:     {self.stats['processed']}")
        print(f"  GitHub найден:              {self.stats['github_found']}")
        print(f"  ORCID есть, GitHub нет:     {self.stats['orcid_no_github']}")
        print(f"  ORCID не нашёлся:           {self.stats['no_orcid']}")
        print(f"  Ошибок ORCID API:           {self.stats['error']}")
        print(f"  Всего ссылок GitHub:        {self.stats['links_total']}")
        print()
        print(f"  Запросов к OpenAlex:        {self.api_stats['openalex_requests']}")
        print(f"  Запросов к ORCID:           {self.api_stats['orcid_requests']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ищет личные GitHub сотрудников ИТМО через ORCID-профили."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Сколько сотрудников обработать за запуск.",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Перепроверить всех заново, а не только новых.",
    )
    parser.add_argument(
        "--source-db", default=str(DB_PATH),
        help="Путь к исходной БД с persons_itmo.",
    )
    parser.add_argument(
        "--out-db", default=str(PROFILES_DB_PATH),
        help="Путь к БД для результатов.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    OrcidGithubFinder(
        source_db=args.source_db,
        out_db=args.out_db,
        limit=args.limit,
        refresh=args.refresh,
    ).run()


if __name__ == "__main__":
    main()
