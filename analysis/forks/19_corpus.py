"""Wide corpus: every repository in the PAUK graph plus every repository of the
organizations listed in the open-source-ops examples.

The lists are markdown files with links to GitHub organizations and single
repositories. Organization links are expanded through the GitHub API; single
repository links are taken as they are. The result is one JSONL file with the
source of every row, so the corpus stays reproducible and auditable.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path

import gh  # noqa: E402

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
RAW = BASE / "raw" / "corpus"
RAW.mkdir(parents=True, exist_ok=True)

LISTS_REPO = "aimclub/open-source-ops"
LISTS_PATH = "examples"
LISTS = ("academic-repositories", "aim-projects", "commercial-os-projects", "pet-projects")
LINK = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?")


def fetch_list(name: str) -> str:
    path = RAW / f"{name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    status, payload = gh.get(f"{gh.API}/repos/{LISTS_REPO}/contents/{LISTS_PATH}/{name}.md")
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"cannot read list {name}: HTTP {status}")
    text = base64.b64decode(payload.get("content") or "").decode("utf-8", "replace")
    path.write_text(text, encoding="utf-8")
    return text


def parse_links(text: str) -> tuple[set[str], set[str]]:
    """Return (organizations, repositories) mentioned in a list."""
    orgs: set[str] = set()
    repos: set[str] = set()
    for owner, name in LINK.findall(text):
        if name in (None, ""):
            orgs.add(owner)
        else:
            repos.add(f"{owner}/{name}")
    orgs -= {r.split("/")[0] for r in repos}
    return orgs, repos


def enumerate_org(owner: str) -> list[dict]:
    """All non-fork repositories of an organization or user account."""
    for kind in ("orgs", "users"):
        rows, _ = gh.paged(f"{gh.API}/{kind}/{owner}/repos", {"type": "public", "sort": "pushed"})
        if rows:
            return [row for row in rows if isinstance(row, dict) and not row.get("fork")]
        # a 404 means the other kind may still exist; retry once for 5xx
        time.sleep(0.2)
    return []


def pauk_rows() -> list[dict]:
    rows = []
    with (DATA / "repositories.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            url = row.get("url") or ""
            match = LINK.fullmatch(url.rstrip("/"))
            if not match or not match.group(2):
                continue
            rows.append({
                "full_name": f"{match.group(1)}/{match.group(2)}",
                "source": "pauk_graph",
                "stars": row.get("stars_num"),
                "forks": row.get("forks_num"),
                "created_at": None,
                "pushed_at": row.get("last_updated"),
                "archived": row.get("archived"),
                "language": row.get("language"),
            })
    return rows


def main() -> None:
    corpus: dict[str, dict] = {row["full_name"].casefold(): row for row in pauk_rows()}
    print(f"PAUK graph: {len(corpus)} repositories")

    for name in LISTS:
        text = fetch_list(name)
        orgs, repos = parse_links(text)
        print(f"{name}: {len(orgs)} organizations, {len(repos)} repositories")
        for full_name in sorted(repos):
            corpus.setdefault(full_name.casefold(), {
                "full_name": full_name,
                "source": f"open-source-ops/{name}",
                "stars": None, "forks": None, "created_at": None,
                "pushed_at": None, "archived": None, "language": None,
            })
        for owner in sorted(orgs):
            rows = enumerate_org(owner)
            print(f"  {owner}: {len(rows)} repositories")
            for row in rows:
                corpus.setdefault(row["full_name"].casefold(), {
                    "full_name": row["full_name"],
                    "source": f"open-source-ops/{name}:{owner}",
                    "stars": row.get("stargazers_count"),
                    "forks": row.get("forks_count"),
                    "created_at": row.get("created_at"),
                    "pushed_at": row.get("pushed_at"),
                    "archived": row.get("archived"),
                    "language": row.get("language"),
                })

    rows = sorted(corpus.values(), key=lambda r: r["full_name"].casefold())
    path = DATA / "corpus.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with_open_forks = sum(1 for row in rows if (row.get("forks") or 0) > 0)
    print(f"corpus: {len(rows)} repositories ({with_open_forks} with forks) -> {path}")


if __name__ == "__main__":
    main()
    sys.exit(0)
