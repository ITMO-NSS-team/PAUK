"""Check the IMPLEMENTS edges against the documents they are built from.

The graph builds IMPLEMENTS from `Repository.publication_ids`; a pair is
legitimate when the publication's link to the repository is relevant.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path("/analysis/forks/data")


def load(name: str) -> list[dict]:
    with (DATA / f"{name}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


repos = {row["id"]: row for row in load("repositories")}
implements = load("implements")
mentions = load("mentions")
links = load("repo_links")

graph_pairs = {(row["repo_id"], row["publication_id"]) for row in implements}
doc_pairs = set()
for repo_id, repo in repos.items():
    for pub_id in repo.get("publication_ids") or []:
        doc_pairs.add((repo_id, pub_id))

print(f"graph IMPLEMENTS pairs: {len(graph_pairs)}")
print(f"document publication_ids pairs: {len(doc_pairs)}")
print(f"graph-only: {len(graph_pairs - doc_pairs)}")
print(f"document-only: {len(doc_pairs - graph_pairs)}")

relevance: dict[tuple[str, str], list] = defaultdict(list)
by_url: dict[str, str] = {}
for repo_id, repo in repos.items():
    for url in repo.get("cited_urls") or []:
        by_url[url.rstrip("/").casefold()] = repo_id
for link in links:
    url = (link.get("url") or "").rstrip("/").casefold()
    repo_id = by_url.get(url)
    if repo_id:
        relevance[(repo_id, link["publication_id"])].append(link.get("is_relevant"))

verdicts = Counter()
for pair in graph_pairs:
    values = relevance.get(pair, [])
    if not values:
        verdicts["no_link_row"] += 1
    elif any(value is True for value in values):
        verdicts["relevant_true"] += 1
    elif any(value is None for value in values):
        verdicts["null"] += 1
    else:
        verdicts["false"] += 1
print("IMPLEMENTS evidence:", dict(verdicts))

mention_pairs = {
    (row["target_id"], row["publication_id"])
    for row in mentions
    if row["target_label"] == "Repository"
}
print(f"MENTIONS_LINK pairs to Repository: {len(mention_pairs)}")
print(f"pairs both mentioned and implemented: {len(mention_pairs & graph_pairs)}")

empty = [repo_id for repo_id, repo in repos.items() if not (repo.get("publication_ids") or [])]
print(f"repositories without publication_ids: {len(empty)}")
