"""Show the evidence behind every derivative candidate."""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path("/analysis/forks/data")
OUT = Path("/analysis/forks/out")

forks = json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8"))
compares = {
    (row["upstream"], row["fork_owner"]): row
    for row in json.loads((DATA / "fork_compare.json").read_text(encoding="utf-8"))
}

for fork in forks:
    if fork["category"] != "derivative":
        continue
    raw = compares.get((fork["upstream"], fork["fork_owner"])) or {}
    files = raw.get("files") or []
    commits = raw.get("commits") or []
    own = [c for c in commits if (c.get("login") or "").casefold() == fork["fork_owner"].casefold()]
    print("=" * 100)
    print(f"{fork['fork_owner']}/{fork['fork_name']}  <-  {fork['upstream']}  ({fork['independence']})")
    print(f"  ahead={fork['ahead_by']} behind={fork['behind_by']} own_commits={len(own)} code_files={len(fork['code_files'])} additions={fork['code_additions']}")
    print(f"  created={fork['fork_created_at']} first_own_commit={fork['first_own_commit']}")
    print(f"  files: {', '.join((f['filename'] + ':' + str(f['status'])) for f in files[:12])}")
    for commit in own[:6]:
        print(f"    {commit['date'][:10]} {commit['login']}: {commit['message'][:110]}")
