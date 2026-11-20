"""Where the named FEDOT-derived repositories sit in the PAUK data."""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def load(name: str) -> list[dict]:
    with (DATA / f"{name}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


repos = load("repositories")
pubs = {row["id"]: row for row in load("publications")}
links = load("repo_links")
mentions = load("mentions")
implements = load("implements")

TARGETS = [
    "aimclub/FEDOT.LLM",
    "aimclub/Fedot.Industrial",
    "ITMO-NSS-team/FEDOT.MAS-Demo",
    "ITMO-NSS-team/FEDOT.Algs",
    "ITMO-NSS-team/FEDOT-benchmarks",
    "ITMO-NSS-team/Fedot-TS-Benchmark",
    "aimclub/FEDOT",
]

by_url = {}
for repo in repos:
    for url in [repo.get("url")] + (repo.get("cited_urls") or []):
        if url:
            by_url[url.rstrip("/").casefold()] = repo

mention_by_pub: dict[str, list] = {}
for row in mentions:
    mention_by_pub.setdefault(row["publication_id"], []).append(row)

for target in TARGETS:
    repo = by_url.get(f"https://github.com/{target}".casefold())
    print("=" * 80)
    print(target)
    if not repo:
        print("  нет строки Repository в PAUK")
        continue
    print(f"  id={repo['id']} is_fork={repo.get('is_fork')} stars={repo.get('stars_num')} forks={repo.get('forks_num')} lang={repo.get('language')}")
    print(f"  publication_ids={len(repo.get('publication_ids') or [])}")
    for pub_id in (repo.get("publication_ids") or [])[:5]:
        pub = pubs.get(pub_id) or {}
        print(f"    IMPLEMENTS <- {pub_id} ({pub.get('year')}) {str(pub.get('title'))[:70]}")
    cited = links and [row for row in links if (row.get("url") or "").rstrip("/").casefold() == f"https://github.com/{target}".casefold()]
    print(f"  строк repo_links по этому URL: {len(cited)}")
    for row in cited[:5]:
        print(f"    {row['publication_id']} is_relevant={row['is_relevant']}")
    impl = [row for row in implements if row["repo_id"] == repo["id"]]
    print(f"  IMPLEMENTS в графе: {len(impl)}")
