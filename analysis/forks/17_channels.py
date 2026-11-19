"""Feasibility of a reuse target built from the channels available to PAUK."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

DATA = Path("/analysis/forks/data")
OUT = Path("/analysis/forks/out")

population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])
forks = [f for f in json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8")) if f["repo_id"] in population]
mentions = [json.loads(line) for line in (DATA / "mentions.jsonl").open(encoding="utf-8")]
implements = [json.loads(line) for line in (DATA / "implements.jsonl").open(encoding="utf-8")]

changed = [f for f in forks if f["category"] in ("derivative", "modified", "contribution")]
derivative = [f for f in forks if f["category"] == "derivative"]

print("=== channel: forks ===")
print("changed forks:", len(changed), "repos:", len({f['repo_id'] for f in changed}))
print("derivative forks:", len(derivative), "repos:", len({f['repo_id'] for f in derivative}))
by_year = Counter(f["fork_year"] for f in changed if f["fork_year"])
print("changed forks by year:", dict(sorted(by_year.items())))

print("\n=== channel: mentions of ITMO repositories by other publications ===")
pub_of_impl = {}
for row in implements:
    pub_of_impl.setdefault(row["repo_id"], set()).add(row["publication_id"])
itmo_mentions = [
    row for row in mentions
    if row["target_label"] == "Repository" and row["target_id"] in population
]
print("MENTIONS_LINK into ITMO repositories:", len(itmo_mentions))
print("with is_relevant=false:", sum(1 for row in itmo_mentions if row["is_relevant"] is False))
print("distinct repositories:", len({row["target_id"] for row in itmo_mentions}))
print("distinct citing publications:", len({row["publication_id"] for row in itmo_mentions}))
self_mentions = sum(
    1 for row in itmo_mentions
    if row["publication_id"] in pub_of_impl.get(row["target_id"], set())
)
print("cited by a publication that implements the same repository:", self_mentions)

print("\n=== repository-year panel with a broader target ===")
for label, rows in (("any changed fork", changed), ("derivative fork", derivative)):
    events = Counter(f["fork_year"] for f in rows if f["fork_year"])
    print(f"{label}: events {sum(events.values())}, repos {len({f['repo_id'] for f in rows})}, by year {dict(sorted(events.items()))}")

repos_with_fork = len({f["repo_id"] for f in forks})
print(f"\nrepositories with at least one fork: {repos_with_fork} of {len(population)}")
print("repositories without forks:", len(population) - repos_with_fork)
print("share of inactive forks:", f"{100 * sum(1 for f in forks if f['category'] == 'inactive') / len(forks):.1f}%")
