"""Pick the examples quoted in the report."""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "out"
population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])
forks = [f for f in json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8")) if f["repo_id"] in population]

print("--- unavailable (404) ---")
for f in [f for f in forks if f["status"] == 404][:5]:
    print(f"  {f['upstream']} <- {f['fork_owner']}/{f['fork_name']}")

print("--- divergence without owner commits ---")
for f in [f for f in forks if f["category"] == "unknown" and f["status"] == 200]:
    print(f"  {f['upstream']} <- {f['fork_owner']}/{f['fork_name']} ahead={f['ahead_by']} own={f['own_commits']}")

print("--- pushed after creation but no divergence (FEDOT) ---")
count = 0
for f in forks:
    if f["upstream"] == "aimclub/FEDOT" and f["category"] == "inactive" and f["fork_pushed_at"] != f["fork_created_at"]:
        print(f"  {f['fork_url']} created={f['fork_created_at'][:10]} pushed={f['fork_pushed_at'][:10]} ahead=0")
        count += 1
        if count >= 4:
            break

print("--- merged contributions ---")
for f in forks:
    if f["pr_merged"]:
        pr = [p for p in f["prs"] if p.get("merged_at")][0]
        print(f"  {f['upstream']} <- {f['fork_owner']} PR #{pr['number']} {pr['url']}")
