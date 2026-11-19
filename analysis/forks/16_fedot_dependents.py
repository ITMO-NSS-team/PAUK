"""Check the named FEDOT-derived repositories on GitHub: fork status and imports."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, "/analysis/forks")

import gh  # noqa: E402

OUT = Path("/analysis/forks/out")

TARGETS = [
    "aimclub/FEDOT",
    "aimclub/FEDOT.LLM",
    "aimclub/Fedot.Industrial",
    "ITMO-NSS-team/FEDOT.MAS-Demo",
    "ITMO-NSS-team/FEDOT.Alts",
    "ITMO-NSS-team/FEDOT.Algs",
    "ITMO-NSS-team/FEDOT-benchmarks",
    "ITMO-NSS-team/Fedot-TS-Benchmark",
]
FILES = ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "environment.yml")


def main() -> None:
    report = []
    for full_name in TARGETS:
        owner, name = full_name.split("/", 1)
        status, meta = gh.get(f"{gh.API}/repos/{owner}/{name}")
        row = {
            "repo": full_name,
            "status": status,
            "exists": status == 200,
            "fork": None,
            "parent": None,
            "stars": None,
            "pushed_at": None,
            "default_branch": None,
            "deps": {},
        }
        if status == 200 and isinstance(meta, dict):
            row.update({
                "fork": meta.get("fork"),
                "parent": (meta.get("parent") or {}).get("full_name"),
                "stars": meta.get("stargazers_count"),
                "pushed_at": meta.get("pushed_at"),
                "default_branch": meta.get("default_branch"),
            })
            for filename in FILES:
                code, payload = gh.get(f"{gh.API}/repos/{owner}/{name}/contents/{filename}")
                if code != 200 or not isinstance(payload, dict):
                    continue
                content = base64.b64decode(payload.get("content") or "").decode("utf-8", "replace")
                hits = [line.strip() for line in content.splitlines() if "fedot" in line.casefold()]
                row["deps"][filename] = hits[:3]
        report.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    status, dependents = gh.get(
        f"{gh.API}/repos/aimclub/FEDOT/dependency-graph/dependents", params={"per_page": 100}
    )
    print(f"FEDOT dependents API: HTTP {status}")
    if status == 200 and isinstance(dependents, dict):
        print("  total:", dependents.get("total_count"), "sample:", [r.get("name") for r in dependents.get("repositories") or []][:10])
        OUT.joinpath("fedot_dependents.json").write_text(json.dumps(dependents, ensure_ascii=False), encoding="utf-8")
    OUT.joinpath("fedot_family_github.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
