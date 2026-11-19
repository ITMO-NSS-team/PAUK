"""Look for pull requests opened from each fork back into its upstream."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/analysis/forks")

import gh  # noqa: E402

DATA = Path("/analysis/forks/data")


def main() -> None:
    rows = json.loads((DATA / "fork_compare.json").read_text(encoding="utf-8"))
    results = []
    for index, row in enumerate(rows, start=1):
        if (row.get("ahead_by") or 0) <= 0:
            continue
        owner = row["upstream_owner"]
        repo = row["upstream"].split("/", 1)[1]
        head = f"{row['fork_owner']}:{row['fork_branch'] or 'main'}"
        key = f"pulls__{row['fork_owner']}__{repo}".lower()

        def fetch():
            status, payload = gh.get(
                f"{gh.API}/repos/{owner}/{repo}/pulls",
                params={"state": "all", "head": head, "per_page": 100},
            )
            if status != 200 or not isinstance(payload, list):
                return {"status": status, "pulls": []}
            return {
                "status": status,
                "pulls": [
                    {
                        "number": item.get("number"),
                        "state": item.get("state"),
                        "merged_at": item.get("merged_at"),
                        "created_at": item.get("created_at"),
                        "title": item.get("title"),
                        "url": item.get("html_url"),
                    }
                    for item in payload
                ],
            }

        record = gh.cached(key, fetch)
        results.append({**{k: row[k] for k in ("upstream", "fork_owner", "fork_name")}, **record})
        print(f"[{index}/{len(rows)}] {row['fork_owner']}: {len(record['pulls'])} PR", flush=True)

    (DATA / "fork_pulls.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    print(f"fork rows with PR lookup: {len(results)}")


if __name__ == "__main__":
    main()
