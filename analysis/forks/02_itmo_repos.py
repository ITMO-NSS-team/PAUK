"""Choose the ITMO scientific repositories whose forks are analysed.

A repository is ITMO's when its owner is an ITMO account by one of the
project's own signals: a Person.github match, the curated organization
catalog, or ITMO in the organization login.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

DATA = Path("/analysis/forks/data")
CATALOG = Path("/analysis/itmo_github_orgs.json")
ITMO_TEXT = re.compile(r"(?<!\w)itmo(?!\w)", re.IGNORECASE)


def load(name: str) -> list[dict]:
    with (DATA / f"{name}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


repos = {row["id"]: row for row in load("graph_repositories")}
mongo_repos = {row["id"]: row for row in load("repositories")}
persons_github = {row["github"].casefold() for row in load("graph_persons_github") if row.get("github")}
catalog = {
    item["login"].casefold()
    for item in json.loads(CATALOG.read_text(encoding="utf-8"))["organizations"]
}
implemented = Counter()
for row in load("implements"):
    implemented[row["repo_id"]] += 1

profiles = {
    (row.get("login") or "").casefold(): row for row in load("graph_profiles")
}

rows = []
for repo_id, count in implemented.items():
    repo = dict(repos.get(repo_id) or {})
    repo.update({k: v for k, v in (mongo_repos.get(repo_id) or {}).items() if k not in repo})
    url = repo.get("url") or ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    owner = (repo.get("owner_login") or (parts[0] if parts else "")).casefold()
    name = repo.get("name") or (parts[1] if len(parts) > 1 else "")
    sources = []
    if owner in persons_github:
        sources.append("person_github")
    if owner in catalog:
        sources.append("org_catalog")
    if "itmo" in owner:
        sources.append("itmo_in_login")
    profile = profiles.get(owner) or {}
    profile_text = " ".join(
        str(profile.get(key) or "") for key in ("company", "location", "name")
    )
    if ITMO_TEXT.search(profile_text):
        sources.append("profile_itmo")
    repo.update({
        "owner": owner,
        "name": name,
        "implements_count": count,
        "itmo_sources": sources,
        "is_itmo": bool(sources),
    })
    rows.append(repo)

rows.sort(key=lambda r: (-r["implements_count"], r["owner"], r["name"]))
(DATA / "repo_ownership.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

itmo = [r for r in rows if r["is_itmo"]]
other = [r for r in rows if not r["is_itmo"]]
print(f"repositories with IMPLEMENTS: {len(rows)}")
print(f"ITMO-owned: {len(itmo)}")
print(f"not ITMO by the rule: {len(other)}")
print("ownership sources:", dict(Counter(s for r in itmo for s in r["itmo_sources"])))
print("forks_num sum (ITMO):", sum(int(r.get("forks_num") or 0) for r in itmo))
print("repos with forks > 0 (ITMO):", sum(1 for r in itmo if int(r.get("forks_num") or 0) > 0))
print("--- top ITMO owners ---")
for owner, count in Counter(r["owner"] for r in itmo).most_common(12):
    print(f"  {owner}: {count}")
print("--- top non-ITMO owners ---")
for owner, count in Counter(r["owner"] for r in other).most_common(12):
    print(f"  {owner}: {count}")
