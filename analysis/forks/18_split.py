"""Descriptive split: repositories with and without fork-based reuse."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "out"

population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])
forks = [f for f in json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8")) if f["repo_id"] in population]
ownership = {row["id"]: row for row in json.loads((DATA / "repo_ownership.json").read_text(encoding="utf-8"))}
mongo = {row["id"]: row for row in (json.loads(line) for line in (DATA / "repositories.jsonl").open(encoding="utf-8"))}
graph = {row["id"]: row for row in (json.loads(line) for line in (DATA / "graph_repositories.jsonl").open(encoding="utf-8"))}
implements = {}
for row in (json.loads(line) for line in (DATA / "implements.jsonl").open(encoding="utf-8")):
    implements.setdefault(row["repo_id"], set()).add(row["publication_id"])

changed = {f["repo_id"] for f in forks if f["category"] in ("derivative", "modified", "contribution")}
derivative = {f["repo_id"] for f in forks if f["category"] == "derivative"}


def stats(ids, field):
    values = []
    for repo_id in ids:
        repo = graph.get(repo_id) or {}
        doc = mongo.get(repo_id) or {}
        value = repo.get(field, doc.get(field))
        if isinstance(value, (int, float)):
            values.append(value)
    return (round(median(values), 1), len(values)) if values else (None, 0)


groups = {
    "с изменениями": changed,
    "без изменений": population - changed,
}
fields = ["stars_num", "forks_num", "created_at"]
lines = []
for name, ids in groups.items():
    stars = stats(ids, "stars_num")
    forksn = stats(ids, "forks_num")
    pubs = median([len(implements.get(i, ())) for i in ids]) if ids else 0
    languages = {}
    for repo_id in ids:
        lang = (mongo.get(repo_id) or {}).get("language") or "—"
        languages[lang] = languages.get(lang, 0) + 1
    top_lang = sorted(languages.items(), key=lambda kv: -kv[1])[:3]
    lines.append(f"{name}: репозиториев {len(ids)}, медиана stars {stars[0]}, медиана forks_num {forksn[0]}, "
                 f"медиана публикаций {pubs}, языки {top_lang}")

for line in lines:
    print(line)
print("repos with derivative:", len(derivative))
