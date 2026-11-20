"""Print the changed forks with their evidence, plus the unavailable ones."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
forks = json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8"))

changed = [f for f in forks if f["category"] != "inactive" and f["category"] != "unknown"]
changed.sort(key=lambda f: (f["upstream"], -(f["ahead_by"] or 0)))
print(f"{'category':12s} {'independence':14s} {'upstream':40s} {'fork_owner':20s} ahead own code  add new pr")
for f in changed:
    print(
        f"{f['category']:12s} {f['independence']:14s} {f['upstream'][:40]:40s} "
        f"{f['fork_owner'][:20]:20s} {f['ahead_by']:5d} {f['own_commits']:3d} "
        f"{len(f['code_files']):4d} {f['code_additions']:5d} {f['added_files']:3d} {len(f['prs']):2d}"
    )

print("\nunavailable forks:", Counter(f["status"] for f in forks if f["category"] == "unknown"))
for f in forks:
    if f["category"] == "unknown":
        print("  ", f["upstream"], f["fork_owner"], f["status"])
