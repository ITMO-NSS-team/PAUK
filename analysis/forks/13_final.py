"""Final numbers: threshold sensitivity, PR outcomes, lags, coverage."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "out"

population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])
forks = [f for f in json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8")) if f["repo_id"] in population]
panel = json.loads((OUT / "panel.json").read_text(encoding="utf-8"))
repos = {row["repo_id"]: row for row in json.loads((DATA / "forks_by_repo.json").read_text(encoding="utf-8")) if row["repo_id"] in population}
INDEPENDENT = {"external", "itmo_other_team", "unknown"}


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


derivative = [f for f in forks if f["category"] == "derivative"]
print("derivative by threshold (functional lines):")
for threshold in (0, 20, 50, 100, 200):
    count = sum(1 for f in derivative if f["functional_additions"] >= threshold)
    print(f"  >= {threshold}: {count}")

changed = [f for f in forks if f["category"] in ("derivative", "modified", "contribution")]
print("\nPR outcomes:", Counter(
    "merged" if f["pr_merged"] else ("open" if any(p.get("state") == "open" for p in f["prs"]) else "closed")
    for f in forks if f["prs"]
))
print("forks with PR:", sum(1 for f in forks if f["prs"]))
print("forks by category:", dict(Counter(f["category"] for f in forks)))
print("forks with own commits:", sum(1 for f in forks if f["own_commits"]))

lags = sorted(
    (parse(f["first_own_commit"]) - parse(f["fork_created_at"])).days
    for f in forks if f["own_commits"] and parse(f["first_own_commit"]) and parse(f["fork_created_at"])
)
print("\nlag fork->first own commit: n =", len(lags), "median =", lags[len(lags) // 2] if lags else None,
      "min =", lags[0] if lags else None, "max =", lags[-1] if lags else None)
print("same day:", sum(1 for lag in lags if lag == 0), "| within 30 days:", sum(1 for lag in lags if 0 <= lag <= 30))

independent = [f for f in derivative if f["independence"] in INDEPENDENT]
lag_repo = []
for repo_id in {f["repo_id"] for f in independent}:
    created = parse((repos.get(repo_id) or {}).get("created_at"))
    first = min((parse(f["fork_created_at"]) for f in independent if f["repo_id"] == repo_id and parse(f["fork_created_at"])), default=None)
    if created and first:
        lag_repo.append({"repo": f"{repos[repo_id]['owner']}/{repos[repo_id]['name']}", "days": (first - created).days})
print("\nrepo creation -> first independent derivative:")
for row in sorted(lag_repo, key=lambda r: r["days"]):
    print(f"  {row['repo']}: {row['days']} дней ({row['days'] / 365:.1f} лет)")

print("\ncoverage:")
print("  repositories in population:", len(repos))
print("  without forks:", sum(1 for r in repos.values() if not r["forks"]))
print("  forks:", sum(len(r["forks"]) for r in repos.values()))
print("  API errors at repository level:", [(r["owner"], r["name"], r["api_status"]) for r in repos.values() if r["api_status"] != 200])
print("  forks_count != collected:", [(r["owner"], r["name"], r["api_forks"], len(r["forks"])) for r in repos.values() if (r["api_forks"] or 0) != len(r["forks"])])
print("  compare unavailable:", Counter(f["status"] for f in forks if f["category"] == "unknown"))
print("  forks created in 2026:", sum(1 for f in forks if f["fork_year"] == 2026))
print("  panel positives:", sum(p["target_next_year"] for p in panel), "of", len(panel))
print("  repos with positive year:", len({p["repo_id"] for p in panel if p["target_next_year"]}))
