"""Compare repository ownership definitions and their effect on the results."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path("/analysis/forks/data")
OUT = Path("/analysis/forks/out")

rows = json.loads((DATA / "repo_ownership.json").read_text(encoding="utf-8"))
by_id = {row["id"]: row for row in rows}
forks = json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8"))
repos_meta = {row["repo_id"]: row for row in json.loads((DATA / "forks_by_repo.json").read_text(encoding="utf-8"))}
INDEPENDENT = {"external", "itmo_other_team", "unknown"}

github_of_person = {
    row["id"]: row["github"].casefold()
    for row in (json.loads(line) for line in (DATA / "graph_persons_github.jsonl").open(encoding="utf-8"))
    if row.get("github")
}
authored: dict[str, set[str]] = defaultdict(set)
for row in (json.loads(line) for line in (DATA / "authored.jsonl").open(encoding="utf-8")):
    login = github_of_person.get(row["person_id"])
    if login:
        authored[row["publication_id"]].add(login)

mongo_repos = {
    row["id"]: row
    for row in (json.loads(line) for line in (DATA / "repositories.jsonl").open(encoding="utf-8"))
}


def population(predicate):
    return {row["id"] for row in rows if row["is_itmo"] and predicate(row)}


v1 = population(lambda row: True)
v2 = population(lambda row: not (mongo_repos.get(row["id"]) or {}).get("is_fork"))
v3 = population(
    lambda row: not (mongo_repos.get(row["id"]) or {}).get("is_fork")
    and (
        "org_catalog" in row["itmo_sources"]
        or row["owner"]
        in {
            login
            for pub_id in (mongo_repos.get(row["id"]) or {}).get("publication_ids") or []
            for login in authored.get(pub_id, set())
        }
    )
)


def stats(ids):
    subset = [f for f in forks if f["repo_id"] in ids]
    derivative = [f for f in subset if f["category"] == "derivative"]
    independent = [f for f in derivative if f["independence"] in INDEPENDENT]
    return {
        "repositories": len(ids),
        "with_forks": sum(1 for repo_id in ids if (repos_meta.get(repo_id) or {}).get("forks")),
        "forks": len(subset),
        "changed": sum(1 for f in subset if f["category"] in ("derivative", "modified", "contribution")),
        "derivative": len(derivative),
        "independent_derivative": len(independent),
        "repos_with_independent_derivative": len({f["repo_id"] for f in independent}),
        "inactive": sum(1 for f in subset if f["category"] == "inactive"),
        "contribution": sum(1 for f in subset if f["category"] == "contribution"),
        "modified": sum(1 for f in subset if f["category"] == "modified"),
        "unknown": sum(1 for f in subset if f["category"] == "unknown"),
    }


for name, ids in (("V1 all ITMO-owned", v1), ("V2 without repository forks", v2), ("V3 V2 + owner is paper author or org", v3)):
    print(name, json.dumps(stats(ids), ensure_ascii=False))

print("\nrepositories that are themselves forks:", [
    f"{by_id[i]['owner']}/{by_id[i]['name']}" for i in v1 - v2
])
print("\nV3 exclusions among V2:", [
    f"{by_id[i]['owner']}/{by_id[i]['name']}" for i in v2 - v3
][:40])
