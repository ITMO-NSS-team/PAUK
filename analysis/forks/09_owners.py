"""Fetch the GitHub account behind every fork owner once."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/analysis/forks")

import gh  # noqa: E402

DATA = Path("/analysis/forks/data")


def main() -> None:
    repos = json.loads((DATA / "forks_by_repo.json").read_text(encoding="utf-8"))
    owners = sorted({(f["owner"] or "") for r in repos for f in r["forks"]})
    rows = []
    for index, login in enumerate(owners, start=1):
        def fetch(login=login):
            status, payload = gh.get(f"{gh.API}/users/{login}")
            if status != 200 or not isinstance(payload, dict):
                return {"status": status, "user": None}
            return {
                "status": status,
                "user": {
                    "login": payload.get("login"),
                    "type": payload.get("type"),
                    "name": payload.get("name"),
                    "company": payload.get("company"),
                    "location": payload.get("location"),
                    "bio": payload.get("bio"),
                    "blog": payload.get("blog"),
                    "email": payload.get("email"),
                    "created_at": payload.get("created_at"),
                    "public_repos": payload.get("public_repos"),
                    "followers": payload.get("followers"),
                    "html_url": payload.get("html_url"),
                },
            }

        rows.append({"login": login, **gh.cached(f"user__{login}".lower(), fetch)})
        if index % 50 == 0:
            print(f"[{index}/{len(owners)}]", flush=True)
    (DATA / "fork_owners.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(f"owners fetched: {len(rows)}")


if __name__ == "__main__":
    main()
