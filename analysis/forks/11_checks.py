"""Cross-checks used in the report: FEDOT, concentration, pushed_at contrast."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "out"

forks = json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8"))
panel = json.loads((OUT / "panel.json").read_text(encoding="utf-8"))
population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])
forks = [row for row in forks if row["repo_id"] in population]
repos = {
    row["repo_id"]: row
    for row in json.loads((DATA / "forks_by_repo.json").read_text(encoding="utf-8"))
    if row["repo_id"] in population
}
INDEPENDENT = {"external", "itmo_other_team", "unknown"}


def parse(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


print("=== forks per repository (top 10) ===")
for _, row in sorted(repos.items(), key=lambda item: -len(item[1]["forks"]))[:10]:
    print(f"  {row['owner']}/{row['name']}: {len(row['forks'])}")

fedot = [f for f in forks if f["upstream"].casefold() == "aimclub/fedot"]
touched = [f for f in fedot if parse(f["fork_pushed_at"]) and parse(f["fork_created_at"]) and parse(f["fork_pushed_at"]) > parse(f["fork_created_at"])]
print(f"\n=== FEDOT ===\nforks={len(fedot)} pushed_after_created={len(touched)} ahead>0={sum(1 for f in fedot if (f['ahead_by'] or 0) > 0)}")
print("categories:", dict(Counter(f["category"] for f in fedot)))
print("pushed_after_created but no commits ahead:", sum(1 for f in touched if (f["ahead_by"] or 0) == 0))
print("of those with commits ahead, categories:", dict(Counter(f["category"] for f in fedot if (f["ahead_by"] or 0) > 0)))

print("\n=== all forks: pushed_at > created_at vs real divergence ===")
touched_all = [f for f in forks if parse(f["fork_pushed_at"]) and parse(f["fork_created_at"]) and parse(f["fork_pushed_at"]) > parse(f["fork_created_at"])]
print(f"pushed_after_created={len(touched_all)}; with commits ahead={sum(1 for f in touched_all if (f['ahead_by'] or 0) > 0)}")
print("categories among touched:", dict(Counter(f["category"] for f in touched_all)))

print("\n=== repositories with independent derivatives ===")
independent = [f for f in forks if f["category"] == "derivative" and f["independence"] in INDEPENDENT]
by_repo = Counter(f["upstream"] for f in independent)
for name, count in by_repo.most_common():
    print(f"  {name}: {count}")
print("total:", len(independent))

print("\n=== panel ===")
positives = [p for p in panel if p["target_next_year"]]
print(f"rows={len(panel)} positives={len(positives)} share={100*len(positives)/len(panel):.1f}%")
print("positives by year:", dict(sorted(Counter(p["year"] for p in positives).items())))
print("repos:", sorted({p["repo"] for p in positives}))
print("rows per repo:", dict(Counter(Counter(p["repo"] for p in panel).values())))

zero_repos = [repo for repo in {p["repo"] for p in panel} if not any(p["target_next_year"] for p in panel if p["repo"] == repo)]
print(f"repos with rows and no positive year: {len(zero_repos)}")
print("years covered:", min(p['year'] for p in panel), max(p['year'] for p in panel))
print("2026 rows:", sum(1 for p in panel if p['year'] == 2026), "with target from 2027 impossible")
