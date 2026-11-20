"""Measure what every fork actually changed relative to its upstream.

`ahead_by` comes from GitHub's comparison of the upstream branch with the
fork branch: commits present in the fork and absent upstream. Files and commit
authors come from the same response, so one request gives both the size and
the authorship of the divergence.
"""

from __future__ import annotations

import json
from pathlib import Path

import gh  # noqa: E402

RAW = Path(__file__).resolve().parent / "raw"
DATA = Path(__file__).resolve().parent / "data"


def compare_record(fork: dict, upstream_owner: str, upstream_name: str, base: str) -> dict:
    key = f"compare__{fork['owner']}__{upstream_name}__{fork['name']}".lower()
    branch = fork.get("default_branch") or "main"

    def fetch():
        url = f"{gh.API}/repos/{upstream_owner}/{upstream_name}/compare/{base}...{fork['owner']}:{branch}"
        status, payload = gh.get(url)
        if status != 200 or not isinstance(payload, dict):
            return {"status": status, "payload": None}
        files = payload.get("files") or []
        commits = payload.get("commits") or []
        return {
            "status": status,
            "ahead_by": payload.get("ahead_by"),
            "behind_by": payload.get("behind_by"),
            "total_commits": payload.get("total_commits"),
            "files_total": len(files),
            "files": [
                {
                    "filename": item.get("filename"),
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "changes": item.get("changes"),
                }
                for item in files
            ],
            "commits": [
                {
                    "sha": item.get("sha"),
                    "date": ((item.get("commit") or {}).get("author") or {}).get("date"),
                    "login": (item.get("author") or {}).get("login"),
                    "message": (((item.get("commit") or {}).get("message") or "").splitlines() or [""])[0][:200],
                }
                for item in commits
            ],
        }

    return gh.cached(key, fetch)


def main() -> None:
    repos = json.loads((DATA / "forks_by_repo.json").read_text(encoding="utf-8"))
    results = []
    calls = 0
    for index, repo in enumerate(repos, start=1):
        owner = repo["owner"]
        base = repo.get("default_branch") or "main"
        for fork in repo["forks"]:
            record = compare_record(fork, owner, repo["name"], base)
            calls += 1
            results.append({
                "repo_id": repo["repo_id"],
                "upstream": f"{owner}/{repo['name']}",
                "upstream_owner": owner,
                "fork_owner": fork["owner"],
                "fork_name": fork["name"],
                "fork_created_at": fork.get("created_at"),
                "fork_pushed_at": fork.get("pushed_at"),
                "fork_branch": fork.get("default_branch"),
                **record,
            })
        print(f"[{index}/{len(repos)}] {owner}/{repo['name']}: {len(repo['forks'])} forks", flush=True)
    (DATA / "fork_compare.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    ahead = sum(1 for row in results if (row.get("ahead_by") or 0) > 0)
    print(f"forks compared={len(results)} ahead={ahead} calls={calls}")


if __name__ == "__main__":
    main()
