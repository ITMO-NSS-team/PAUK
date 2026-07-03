import argparse
import json
import re
import sqlite3
import time
import unicodedata

import requests
from config import (
    CROSSREF_REQUEST_DELAY,
    CROSSREF_TIMEOUT,
    DB_PATH,
    SQLITE_TIMEOUT,
    USER_AGENT_EMAIL,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crossref_orcid (
    person_id TEXT PRIMARY KEY,
    orcid     TEXT,
    doi       TEXT,
    found_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CROSSREF = "https://api.crossref.org/works/"


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
        toks = [alpha(t) for t in (n or "").split()]
        toks = [t for t in toks if t]
        if len(toks) >= 2 and len(toks[-1]) >= 4:
            surnames.add(toks[-1])
    return surnames


def crossref_authors(session: requests.Session, doi: str) -> list[tuple[str, str]]:
    """[(фамилия_alpha, orcid)] по DOI из Crossref."""
    doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
    try:
        r = session.get(f"{CROSSREF}{doi}", timeout=CROSSREF_TIMEOUT)
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


def surname_match(family: str, surnames: set[str]) -> bool:
    return any(s == family or s in family or family in s for s in surnames)


def run(limit: int) -> None:
    main = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    prof = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    prof.executescript(SCHEMA_SQL)

    have_orcid = {pid for pid, in prof.execute(
        "SELECT person_id FROM person_profiles WHERE orcid > ''")}
    done = {r[0] for r in prof.execute("SELECT person_id FROM crossref_orcid")}

    # Фамилии только для тех, кому ORCID нужен.
    surn: dict[str, set[str]] = {}
    for pid, name_en, variants in main.execute(
        "SELECT id, name_en, name_variants FROM persons_itmo WHERE name_en > ''"
    ):
        if pid in have_orcid or pid in done:
            continue
        s = author_surnames(name_en, variants)
        if s:
            surn[pid] = s

    # Публикации с DOI, где есть нуждающиеся ИТМО-авторы.
    by_pub: dict[tuple[str, str], list[str]] = {}
    for pub_id, pid, doi in main.execute(
        """
        SELECT pa.publication_id, pa.person_id, p.doi
        FROM publication_authors pa JOIN publications p ON p.id = pa.publication_id
        WHERE pa.person_type = 'itmo' AND p.doi > ''
        """
    ):
        if pid in surn:
            by_pub.setdefault((pub_id, doi), []).append(pid)

    pubs = list(by_pub.items())
    if limit:
        pubs = pubs[:limit]
    print(f"Публикаций к проверке: {len(pubs)} (нуждающихся персон: {len(surn)})\n")

    session = requests.Session()
    session.headers["User-Agent"] = f"ITMO-Research/1.0 (mailto:{USER_AGENT_EMAIL})"
    stats = {"pubs": 0, "found": 0}
    assigned: set[str] = set()
    for i, ((pub_id, doi), pids) in enumerate(pubs, 1):
        stats["pubs"] += 1
        need = [pid for pid in pids if pid not in assigned]
        if not need:
            continue
        for family, orc in crossref_authors(session, doi):
            cand = [pid for pid in need if surname_match(family, surn[pid])]
            if len(cand) == 1:
                prof.execute(
                    "INSERT OR IGNORE INTO crossref_orcid (person_id, orcid, doi) VALUES (?, ?, ?)",
                    (cand[0], orc, doi),
                )
                assigned.add(cand[0])
                stats["found"] += 1
        time.sleep(CROSSREF_REQUEST_DELAY)
        if stats["pubs"] % 100 == 0:
            prof.commit()
            print(f"  [{i}/{len(pubs)}] найдено ORCID: {stats['found']}")
    prof.commit()

    total = prof.execute("SELECT COUNT(*) FROM crossref_orcid").fetchone()[0]
    prof.close()
    main.close()
    print()
    print(f"Публикаций проверено:   {stats['pubs']}")
    print(f"Новых ORCID: {stats['found']}")
    print(f"Всего в crossref_orcid:  {total}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ORCID авторов из Crossref -> crossref_orcid.")
    parser.add_argument("--limit", type=int, default=None, help="Сколько публикаций проверить.")
    run(parser.parse_args().limit)


if __name__ == "__main__":
    main()
