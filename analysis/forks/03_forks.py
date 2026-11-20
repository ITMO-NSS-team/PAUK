"""Collect the full fork list of every ITMO scientific repository."""

from __future__ import annotations

import json
from pathlib import Path

import gh  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
RAW = Path(__file__).resolve().parent / "raw"
RAW.mkdir(parents=True, exist_ok=True)


def main() -> None:
    repos = json.loads((DATA / "repo_ownership.json").read_text(encoding="utf-8"))
    itmo = [r for r in repos if r["is_itmo"]]
    itmo.sort(key=lambda r: -int(r.get("forks_num") or 0))

    out = []
    calls = 0
    for index, repo in enumerate(itmo, start=1):
        owner, name = repo["owner"], repo["name"]
        key = f"forks__{owner}__{name}".lower()
        status_meta, meta = gh.get(f"{gh.API}/repos/{owner}/{name}")
        calls += 1
        record = {
            "repo_id": repo["id"],
            "owner": owner,
            "name": name,
            "url": repo.get("url"),
            "forks_num_pauk": repo.get("forks_num"),
            "api_status": status_meta,
            "api_forks": None,
            "default_branch": None,
            "created_at": None,
            "pushed_at": None,
            "forks": [],
            "pages": 0,
            "error": None,
        }
        if status_meta == 200 and isinstance(meta, dict):
            record.update({
                "api_forks": meta.get("forks_count"),
                "default_branch": meta.get("default_branch"),
                "created_at": meta.get("created_at"),
                "pushed_at": meta.get("pushed_at"),
                "language": meta.get("language"),
                "stars": meta.get("stargazers_count"),
                "archived": meta.get("archived"),
                "fork": meta.get("fork"),
            })
            forks, pages = gh.paged(f"{gh.API}/repos/{owner}/{name}/forks", {"sort": "newest"})
            calls += pages
            record["pages"] = pages
            record["forks"] = [
                {
                    "owner": (item.get("owner") or {}).get("login"),
                    "name": item.get("name"),
                    "full_name": item.get("full_name"),
                    "id": item.get("id"),
                    "created_at": item.get("created_at"),
                    "pushed_at": item.get("pushed_at"),
                    "updated_at": item.get("updated_at"),
                    "default_branch": item.get("default_branch"),
                    "size": item.get("size"),
                    "stars": item.get("stargazers_count"),
                    "language": item.get("language"),
                    "fork": item.get("fork"),
                }
                for item in forks
            ]
        else:
            record["error"] = f"HTTP {status_meta}"
        (RAW / f"{key}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        out.append(record)
        print(f"[{index}/{len(itmo)}] {owner}/{name}: {len(record['forks'])} forks, api={record['api_forks']}, pauk={record['forks_num_pauk']}", flush=True)

    (DATA / "forks_by_repo.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    total = sum(len(r["forks"]) for r in out)
    print(f"repos={len(out)} forks={total} calls={calls}")


if __name__ == "__main__":
    main()
