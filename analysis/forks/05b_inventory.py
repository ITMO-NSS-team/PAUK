"""Inventory of the collected fork lists: counts, gaps and errors."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

repos = json.loads((DATA / "forks_by_repo.json").read_text(encoding="utf-8"))
total = sum(len(r["forks"]) for r in repos)
print(f"repositories queried: {len(repos)}")
print(f"forks collected: {total}")
print("API status of repositories:", dict(Counter(r["api_status"] for r in repos)))
failed = [r for r in repos if r["api_status"] != 200]
for row in failed:
    print("  failed:", row["owner"], row["name"], row["error"])

mismatch = [
    (r["owner"], r["name"], r["api_forks"], len(r["forks"]), r["forks_num_pauk"])
    for r in repos
    if (r["api_forks"] or 0) != len(r["forks"])
]
print(f"\nrepositories where forks_count != collected: {len(mismatch)}")
for owner, name, api, got, pauk in mismatch:
    print(f"  {owner}/{name}: api={api} collected={got} pauk={pauk}")

stale = [
    (r["owner"], r["name"], r["forks_num_pauk"], r["api_forks"])
    for r in repos
    if r["forks_num_pauk"] is not None and r["api_forks"] != r["forks_num_pauk"]
]
print(f"\nrepositories where PAUK forks_num differs from live count: {len(stale)}")
print(f"  sum PAUK={sum(int(s[2] or 0) for s in stale)} sum live={sum(int(s[3] or 0) for s in stale)}")

archived = [(r["owner"], r["name"]) for r in repos if r.get("archived")]
print(f"\narchived upstream repositories: {len(archived)} {archived[:5]}")

owners = Counter(f["owner"] for r in repos for f in r["forks"])
print(f"\ndistinct fork owners: {len(owners)}")
print("repeated owners:", [(o, n) for o, n in owners.most_common(10) if n > 1])
