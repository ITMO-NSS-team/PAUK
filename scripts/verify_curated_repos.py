"""Check every row of the curated CSV against what the graph actually holds.

Writes a CSV with one row per input row and a verdict for each: is the
repository a node, is the paper a node, is there an edge between them. Reads
only — nothing here changes a database.

Deliberately does *not* reuse the matching code from
`import_curated_repos.py`. A verifier that shares its subject's logic cannot
catch a bug in that logic: if title normalization were wrong, both would be
wrong the same way and the report would look clean. The two implementations
agreeing is itself part of the evidence.

Run where Neo4j is reachable (the server), then fetch the CSV.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import logging
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from neo4j import GraphDatabase

from pauk.settings import settings

logger = logging.getLogger("verify_curated_repos")

REPO_URL = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)
FUZZY_CUTOFF = 0.90

YES, NO = "да", "нет"

# The report's columns, in order. Named here rather than read off the first
# built row so that a run over an empty CSV still writes a usable header.
REPORT_COLUMNS = [
    "title", "repo_url", "confidence", "note", "статус", "репозиторий_в_графе",
    "url_в_графе", "переименован", "id_узла_репозитория", "статья_в_графе",
    "id_статьи", "год", "заголовок_в_графе", "совпадение_заголовка",
    "связь_IMPLEMENTS", "связь_MENTIONS_LINK", "пояснение",
]


def fold(text: str) -> str:
    """Letters and digits only — what two records of one paper always share."""
    folded = unicodedata.normalize("NFKD", text or "").lower()
    return " ".join(re.sub(r"[^0-9a-zа-яё]+", " ", folded).split())


def canonical_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def load_graph(session) -> tuple[dict, dict, dict, set, set]:
    """Everything the verdicts need, in five lookups instead of 259 × 4 queries."""
    repos_by_url: dict[str, dict] = {}
    repos_by_id: dict[str, dict] = {}
    for record in session.run(
            "MATCH (r:Repository) RETURN r.id AS id, r.url AS url, r.cited_urls AS cited"):
        repo = {"id": record["id"], "url": record["url"]}
        repos_by_id[record["id"]] = repo
        for url in [record["url"], *(record["cited"] or [])]:
            if url:
                repos_by_url.setdefault(canonical_url(url), repo)

    pubs_by_title: dict[str, list[dict]] = defaultdict(list)
    for record in session.run(
            "MATCH (p:Publication) RETURN p.id AS id, p.title AS title, p.year AS year"):
        key = fold(record["title"])
        if key:
            pubs_by_title[key].append(
                {"id": record["id"], "title": record["title"], "year": record["year"]})

    implements = {
        (r["repo"], r["pub"])
        for r in session.run(
            "MATCH (repo:Repository)-[:IMPLEMENTS]->(pub:Publication) "
            "RETURN repo.id AS repo, pub.id AS pub")
    }
    mentions = {
        (r["repo"], r["pub"])
        for r in session.run(
            "MATCH (pub:Publication)-[:MENTIONS_LINK]->(repo:Repository) "
            "RETURN repo.id AS repo, pub.id AS pub")
    }
    return repos_by_url, repos_by_id, pubs_by_title, implements, mentions


def find_publication(title: str, pubs_by_title: dict, keys: list) -> tuple[dict | None, str]:
    """Publication for a curated title, and how it was matched.

    An ambiguous title resolves to nothing on purpose: the curated row says
    nothing that could choose between two papers sharing a name.
    """
    key = fold(title)
    hits = pubs_by_title.get(key)
    if hits and len(hits) == 1:
        return hits[0], "точное"
    if hits:
        return None, "неоднозначное"
    close = difflib.get_close_matches(key, keys, n=1, cutoff=FUZZY_CUTOFF)
    if not close:
        return None, "—"
    hits = pubs_by_title[close[0]]
    return (hits[0], "близкое") if len(hits) == 1 else (None, "неоднозначное")


def verdict(row: dict, repo: dict | None, pub: dict | None, implements: set,
            mentions: set) -> tuple[str, str]:
    """Short status plus the reason behind it."""
    url = (row["repo_url"] or "").strip()
    if not url or url.lower() == "none":
        return "нет ссылки", "в строке CSV не указан репозиторий"
    if not REPO_URL.match(url):
        return "не репозиторий", "ссылка ведёт на организацию или GitHub Pages, а не на репозиторий"
    if repo is None:
        return "не загружен", "репозитория нет в графе"
    if pub is None:
        return "статьи нет", "репозиторий в графе есть, но публикации с таким заголовком нет"
    if (repo["id"], pub["id"]) in implements:
        return "связан", "репозиторий и статья в графе, связь IMPLEMENTS есть"
    if (repo["id"], pub["id"]) in mentions:
        return "только упоминание", "связи IMPLEMENTS нет, но статья ссылается на репозиторий (MENTIONS_LINK)"
    return "не связан", "репозиторий и статья в графе, но связи между ними нет"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=Path("itmo-github-repos.csv"))
    parser.add_argument("--out", type=Path, default=Path("data/reports/curated-repos-verified.csv"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with args.csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    logger.info("rows in csv: %d", len(rows))

    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    with driver.session(default_access_mode="READ") as session:
        repos_by_url, repos_by_id, pubs_by_title, implements, mentions = load_graph(session)
    driver.close()
    logger.info("graph: %d repositories, %d distinct publication titles, %d IMPLEMENTS",
                len(repos_by_id), len(pubs_by_title), len(implements))

    keys = list(pubs_by_title)
    out_rows = []
    for row in rows:
        url = (row["repo_url"] or "").strip()
        match = REPO_URL.match(url)
        repo = None
        if match:
            derived = f"github_{match.group(1).lower()}_{match.group(2).lower()}"
            repo = repos_by_url.get(canonical_url(url)) or repos_by_id.get(derived)
        pub, how = find_publication(row["title"], pubs_by_title, keys)
        status, reason = verdict(row, repo, pub, implements, mentions)

        linked = bool(repo and pub and (repo["id"], pub["id"]) in implements)
        mentioned = bool(repo and pub and (repo["id"], pub["id"]) in mentions)
        renamed = bool(repo and match and canonical_url(repo["url"]) != canonical_url(url))
        out_rows.append({
            "title": row["title"],
            "repo_url": row["repo_url"],
            "confidence": row["confidence"],
            "note": row["note"],
            "статус": status,
            "репозиторий_в_графе": YES if repo else NO,
            "url_в_графе": repo["url"] if repo else "",
            "переименован": YES if renamed else NO,
            "id_узла_репозитория": repo["id"] if repo else "",
            "статья_в_графе": YES if pub else NO,
            "id_статьи": pub["id"] if pub else "",
            "год": pub["year"] if pub else "",
            "заголовок_в_графе": pub["title"] if pub and fold(pub["title"]) != fold(row["title"]) else "",
            "совпадение_заголовка": how,
            "связь_IMPLEMENTS": YES if linked else NO,
            "связь_MENTIONS_LINK": YES if mentioned else NO,
            "пояснение": reason,
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        # Fixed column list, not list(out_rows[0]): a CSV holding nothing but
        # a header is a legitimate input, and it must produce an empty report
        # rather than an IndexError.
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)
    logger.info("written: %s", args.out)

    counts = Counter(r["статус"] for r in out_rows)
    print(f"\nстрок: {len(out_rows)}")
    for status, count in counts.most_common():
        print(f"  {status:20} {count}")
    repos = {r["id_узла_репозитория"] for r in out_rows if r["id_узла_репозитория"]}
    print(f"\nразличных репозиториев из CSV в графе: {len(repos)}")
    print(f"строк со связью IMPLEMENTS: {sum(1 for r in out_rows if r['связь_IMPLEMENTS'] == YES)}")


if __name__ == "__main__":
    sys.exit(main())
