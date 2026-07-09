import argparse
import json
import logging
import re
import sqlite3
import time

import requests
from config import (
    DB_PATH,
    HTTP_TIMEOUT,
    OPENREVIEW_API_URL,
    OPENREVIEW_PASSWORD,
    OPENREVIEW_RATE_LIMIT_SLEEP,
    OPENREVIEW_REQUEST_DELAY,
    OPENREVIEW_USERNAME,
    SQLITE_TIMEOUT,
)

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS openreview_profiles (
    person_id     TEXT PRIMARY KEY,
    openreview_id TEXT,
    name_en       TEXT,
    matched_by    TEXT,   -- orcid | itmo_email | itmo_affil
    names         TEXT,   -- JSON [str]
    emails_masked TEXT,   -- JSON [str]
    affiliations  TEXT,   -- JSON [{name, position, start, end}]
    relations     TEXT,   -- JSON [{relation, name}]
    homepage      TEXT,
    gscholar      TEXT,
    dblp          TEXT,
    orcid         TEXT,
    github        TEXT,
    linkedin      TEXT,
    found_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_orp_oid ON openreview_profiles(openreview_id);
"""

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
    def __init__(self) -> None:
        self.s = requests.Session()
        r = self.s.post(
            f"{OPENREVIEW_API_URL}/login",
            json={"id": OPENREVIEW_USERNAME, "password": OPENREVIEW_PASSWORD},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['token']}"
        self.calls = 0

    def search(self, term: str) -> list[dict]:
        self.calls += 1
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


class OpenReviewEnricher:
    def __init__(self, limit, refresh) -> None:
        self.limit = limit
        self.refresh = refresh
        self.gh = OpenReviewClient()
        self.stats = {"processed": 0, "matched": 0,
                      "orcid": 0, "itmo_email": 0, "itmo_affil": 0}

    def load_people(self, out):
        main = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
        prof = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
        pdata = {pid: (tail(o), jloads(on)) for pid, o, on in prof.execute(
            "SELECT person_id, orcid, other_names FROM person_profiles")}
        done = {r[0] for r in out.execute("SELECT person_id FROM openreview_profiles")}
        people = []
        for pid, name_en, variants in main.execute(
            "SELECT id, name_en, name_variants FROM persons_itmo WHERE name_en > '' ORDER BY id"
        ):
            if not self.refresh and pid in done:
                continue
            our_orcid, others = pdata.get(pid, (None, []))
            norms = {norm(name_en)} | {norm(n) for n in jloads(variants) + others}
            extra = [n for n in others if norm(n) and norm(n) != norm(name_en)]
            people.append((pid, name_en, {n for n in norms if n}, extra, our_orcid))
            if self.limit and len(people) >= self.limit:
                break
        main.close()
        prof.close()
        return people

    def find(self, name_en, name_norms, extra, our_orcid):
        for term in list(search_terms(name_en)) + extra:
            for profile in self.gh.search(term):
                m = verify(profile.get("content", {}), name_norms, our_orcid)
                if m:
                    return profile, m
            time.sleep(OPENREVIEW_REQUEST_DELAY)
        return None, None

    def save(self, out, pid, data):
        out.execute(
            """
            INSERT OR REPLACE INTO openreview_profiles
                (person_id, openreview_id, name_en, matched_by, names, emails_masked,
                 affiliations, relations, homepage, gscholar, dblp, orcid, github, linkedin)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (pid, data["openreview_id"], data["name_en"], data["matched_by"],
             json.dumps(data["names"], ensure_ascii=False),
             json.dumps(data["emails_masked"], ensure_ascii=False),
             json.dumps(data["affiliations"], ensure_ascii=False),
             json.dumps(data["relations"], ensure_ascii=False),
             data["homepage"], data["gscholar"], data["dblp"],
             data["orcid"], data["github"], data["linkedin"]),
        )

    def run(self) -> None:
        out = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
        out.executescript(SCHEMA_SQL)
        people = self.load_people(out)
        logger.info("К обработке: %d персон", len(people))
        try:
            for i, (pid, name_en, norms, extra, our_orcid) in enumerate(people, 1):
                self.stats["processed"] += 1
                profile, matched_by = self.find(name_en, norms, extra, our_orcid)
                if profile:
                    data = extract(profile, matched_by, name_en)
                    self.save(out, pid, data)
                    self.stats["matched"] += 1
                    self.stats[matched_by] += 1
                    logger.info("[%d/%d] %s -> %s (%s)", i, len(people), name_en[:28], data['openreview_id'], matched_by)
                if self.stats["processed"] % 20 == 0:
                    out.commit()
            out.commit()
        except KeyboardInterrupt:
            logger.warning("Прервано пользователем")
            out.commit()
        finally:
            self.summary()
            out.close()

    def summary(self) -> None:
        logger.info("Обработано: %d, Найдено профилей: %d по orcid: %d по @itmo.ru: %d по аффилиации: %d, Запросов к OpenReview: %d", self.stats['processed'], self.stats['matched'], self.stats['orcid'], self.stats['itmo_email'], self.stats['itmo_affil'], self.gh.calls)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Сбор профилей OpenReview -> openreview_profiles.")
    p.add_argument("--limit", type=int, default=None, help="Сколько персон обработать.")
    p.add_argument("--refresh", action="store_true", help="Пересобрать всех.")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    OpenReviewEnricher(limit=args.limit, refresh=args.refresh).run()


if __name__ == "__main__":
    main()
