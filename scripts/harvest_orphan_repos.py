"""Collect the people behind repositories the enrichment stage cannot reach.

`RepositoriesStage` walks `repo_links` and harvests the repositories those
links name (`repositories.py:168`). A repository created by any other route —
the curated CSV import, a manual addition — has no link row pointing at it and
is therefore never visited, with or without `--force`. This closes that gap
and nothing else.

The harvest itself is the stage's own `_harvest_accounts`: reusing it is the
point. A second implementation of "who is behind this repository" would drift
from the one the pipeline actually runs, and the difference would show up as
data, not as a failing test.

Writes with `upsert_models`, never `write_models`. The latter sets a group's
complete membership, so handing it a subset would retract the group's claim on
every repository not in that subset and delete the ones no other group holds.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from pauk.models import GitHubProfile, RepoLink, Repository
from pauk.pipeline.stages.repositories import GITHUB_HOSTS, RepositoriesStage
from pauk.settings import settings
from pauk.sources.github import GitHubClient
from pauk.storage import PreparedStore, RawStore
from pauk.storage.mongo import get_mongo_client

logger = logging.getLogger("harvest_orphan_repos")


def repo_id_from_url(url: str) -> str | None:
    """`github_{owner}_{name}` for a plain repository URL, else None."""
    parsed = urlparse((url or "").rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc.lower() not in GITHUB_HOSTS or len(parts) != 2:
        return None
    return f"github_{parts[0].lower()}_{parts[1].lower()}"


def reachable_ids(db) -> set[str]:
    """Repository ids some `repo_links` row names, across every group.

    Group-wide and not per-group on purpose: a repository the stage reaches
    while running a different group is not an orphan, it is merely harvested
    later.
    """
    found: set[str] = set()
    for row in db[PreparedStore.COLLECTIONS["repo_links"]].find({}, {"links": 1}):
        for link in row.get("links") or []:
            repo_id = repo_id_from_url(link.get("url") or "")
            if repo_id:
                found.add(repo_id)
    return found


def owner_and_name(repo: Repository) -> tuple[str, str] | None:
    """Owner and name to query GitHub with, preferring the canonical URL."""
    parsed = urlparse((repo.url or "").rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc.lower() in GITHUB_HOSTS and len(parts) == 2:
        return parts[0], parts[1]
    if repo.owner_login and repo.name:
        return repo.owner_login, repo.name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be harvested and make no requests")
    parser.add_argument("--force", action="store_true",
                        help="re-harvest even repositories that already have contributors")
    parser.add_argument("--limit", type=int, help="stop after this many repositories")
    parser.add_argument("--report", type=Path, default=Path("data/reports/orphan-harvest.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = get_mongo_client(settings)
    db = client[settings.mongo_db]
    try:
        reachable = reachable_ids(db)
        repo_collection = db[PreparedStore.COLLECTIONS["repositories"]]

        # Each orphan is harvested once, under a group it already belongs to,
        # so the write adds no group tag the repository did not already carry.
        by_group: dict[str, list[Repository]] = defaultdict(list)
        seen: set[str] = set()
        for doc in repo_collection.find({}):
            if doc["_id"] in reachable or doc["_id"] in seen:
                continue
            groups = doc.get("groups") or []
            if not groups:
                logger.warning("skipping %s: belongs to no group", doc["_id"])
                continue
            seen.add(doc["_id"])
            payload = {k: v for k, v in doc.items() if k not in ("_id", "groups", "_version")}
            payload["id"] = doc["_id"]
            by_group[sorted(groups)[0]].append(Repository.model_validate(payload))

        # Profiles from every group, not just the one being written. The stage
        # loads its own group's only, so a profile another group discovered an
        # address on would be rebuilt without it; here the merge sees it.
        profiles = {
            doc["_id"]: GitHubProfile.model_validate(
                {**{k: v for k, v in doc.items() if k not in ("_id", "groups", "_version")},
                 "id": doc["_id"]})
            for doc in db[PreparedStore.COLLECTIONS["github_profiles"]].find({})
        }

        total = sum(len(v) for v in by_group.values())
        logger.info("orphan repositories: %d across %d group(s)", total, len(by_group))
        for group, repos in sorted(by_group.items()):
            already = sum(1 for r in repos if r.contributors)
            logger.info("  %-42s %3d (%d already harvested)", group, len(repos), already)

        if args.dry_run:
            for group, repos in sorted(by_group.items()):
                for repo in repos:
                    print(f"  {group:<42} {repo.id:<60} {'harvested' if repo.contributors else '-'}")
            return 0

        github = GitHubClient(settings.request_timeout, settings.github_token)
        processed: list[dict] = []
        budget = args.limit
        for group, repos in sorted(by_group.items()):
            prepared = PreparedStore(db, group)
            raw = RawStore(db, group)
            stage = RepositoriesStage(prepared, raw, settings)
            touched: list[Repository] = []
            for repo in repos:
                if budget is not None and budget <= 0:
                    break
                if repo.contributors and not args.force:
                    continue
                target = owner_and_name(repo)
                if target is None:
                    logger.warning("skipping %s: no owner/name to query", repo.id)
                    continue
                owner, name = target
                before = list(repo.contributors)
                known = profiles.get(f"github_{(repo.owner_login or '').lower()}")
                stage._harvest_accounts(github, repo, owner, name,
                                        known.type if known else None, profiles)
                touched.append(repo)
                processed.append({"group": group, "id": repo.id, "url": repo.url,
                                  "contributors_before": len(before),
                                  "contributors_after": len(repo.contributors)})
                logger.info("%-60s %d contributor(s)", repo.id, len(repo.contributors))
                if budget is not None:
                    budget -= 1
            if touched:
                prepared.upsert_models("repositories", touched)
                # Only the profiles this group's harvest touched: upserting all
                # of them would tag every profile with this group.
                logins = {login for repo in touched for login in repo.contributors}
                prepared.upsert_models(
                    "github_profiles",
                    [profiles[f"github_{login.lower()}"] for login in sorted(logins)
                     if f"github_{login.lower()}" in profiles])
    finally:
        client.close()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(
        {"created_at": datetime.now(UTC).isoformat(), "repositories": processed},
        ensure_ascii=False, indent=2), encoding="utf-8")
    harvested = sum(1 for r in processed if r["contributors_after"])
    print(f"\nprocessed: {len(processed)}   with contributors: {harvested}   "
          f"empty: {len(processed) - harvested}")
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
