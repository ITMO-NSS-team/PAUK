"""Remove IMPLEMENTS claims that came from links judged to be someone else's tool.

The fix in `repositories.py` stops new ones from appearing, but it cannot undo
what is already stored: `publication_ids` only ever grows — the stage appends
to it and never clears it — so every claim recorded before the fix stays until
something removes it.

The rule is subtractive on purpose. A publication id is dropped only when
*every* link from that publication to this repository was judged
`is_relevant=False`; anything else is left exactly as it is. That matters
because `publication_ids` has a second source: repositories imported from the
curated CSV carry ids that no `repo_links` row mentions at all. Recomputing the
field from `repo_links` would silently erase those curated claims, which is why
this walks the removals instead of rebuilding the list.

Reads every group at once. A repository can be cited by publications in several
groups, so a per-group pass would judge a link absent merely because it belongs
to another group's rows.

Dry run by default; `--apply` writes, through `upsert_models` — never
`write_models`, which would set the whole group's membership from a subset.
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

from pauk.models import Repository
from pauk.pipeline.stages.repositories import GITHUB_HOSTS
from pauk.settings import settings
from pauk.storage import PreparedStore
from pauk.storage.mongo import get_mongo_client
from pauk.urls import normalize_repo_url

logger = logging.getLogger(__name__)


def repo_id_from_url(url: str) -> str | None:
    parsed = urlparse((url or "").rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc.lower() not in GITHUB_HOSTS or len(parts) != 2:
        return None
    return f"github_{parts[0].lower()}_{parts[1].lower()}"


def url_to_repo_id(db) -> dict[str, str]:
    """Every URL a stored repository is known by, mapped to its stored id.

    The id cannot be guessed from the cited URL. A repository renamed on
    GitHub is re-keyed to the canonical `github_{owner}_{name}` the API
    answers with (`repositories._canonical_repo_id`), while the URL the paper
    cited still carries the old name — deriving an id from that URL would
    look up a row that no longer exists, and the stale claim would survive
    the repair. `cited_urls` is what ties the two together: it keeps every
    URL that ever produced the row.
    """
    index: dict[str, str] = {}
    for doc in db[PreparedStore.COLLECTIONS["repositories"]].find({}, {"url": 1, "cited_urls": 1}):
        for url in [doc.get("url"), *(doc.get("cited_urls") or [])]:
            if url:
                index[normalize_repo_url(url)] = doc["_id"]
    return index


def irrelevant_claims(db) -> dict[str, set[str]]:
    """Per repository, the publications whose every link to it was judged False.

    A publication that links the same repository twice — once as a dependency
    and once as its own code — keeps the claim: one relevant link is enough.
    """
    known = url_to_repo_id(db)
    verdicts: dict[str, dict[str, set[bool | None]]] = defaultdict(lambda: defaultdict(set))
    for row in db[PreparedStore.COLLECTIONS["repo_links"]].find({}, {"publication_id": 1, "links": 1}):
        publication = row.get("publication_id")
        if not publication:
            continue
        for link in row.get("links") or []:
            url = link.get("url") or ""
            # The stored row wins; the URL-derived id is only for links that
            # never became a repository row at all, which own no claim anyway.
            repo_id = known.get(normalize_repo_url(url)) or repo_id_from_url(url)
            if repo_id:
                verdicts[repo_id][publication].add(link.get("is_relevant"))
    return {
        repo_id: {pub for pub, seen in per_pub.items() if seen == {False}}
        for repo_id, per_pub in verdicts.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the removals (default: dry run)")
    parser.add_argument("--report", type=Path, default=Path("data/reports/repair-implements.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = get_mongo_client(settings)
    db = client[settings.mongo_db]
    changes: list[dict] = []
    skipped: list[str] = []
    complete = False
    try:
        drop = irrelevant_claims(db)
        collection = db[PreparedStore.COLLECTIONS["repositories"]]
        for doc in collection.find({}):
            stored = list(doc.get("publication_ids") or [])
            remove = [p for p in stored if p in drop.get(doc["_id"], ())]
            if remove:
                changes.append({"id": doc["_id"], "url": doc.get("url"), "groups": doc.get("groups"),
                                "before": stored, "removed": remove,
                                "after": [p for p in stored if p not in set(remove)]})

        removed_total = sum(len(c["removed"]) for c in changes)
        emptied = sum(1 for c in changes if not c["after"])
        print(f"repositories touched: {len(changes)}")
        print(f"IMPLEMENTS claims removed: {removed_total}")
        print(f"repositories left with no IMPLEMENTS at all: {emptied}")
        for c in sorted(changes, key=lambda x: -len(x["removed"]))[:12]:
            print(f"  -{len(c['removed']):<3} {c['url']}")

        if args.apply:
            by_group: dict[str, list[Repository]] = defaultdict(list)
            for c in changes:
                doc = collection.find_one({"_id": c["id"]})
                groups = doc.get("groups") or []
                # A row belonging to no group has no group to be written back
                # under, and upsert_models tags whatever group it is given —
                # a placeholder would land in `groups` as a real tag. Left
                # alone and reported instead, as harvest_orphan_repos does.
                if not groups:
                    logger.warning("skipping %s: belongs to no group", doc["_id"])
                    skipped.append(doc["_id"])
                    continue
                payload = {k: v for k, v in doc.items() if k not in ("_id", "groups", "_version")}
                payload["id"] = doc["_id"]
                payload["publication_ids"] = c["after"]
                # Written under a group the repository already belongs to, so
                # the upsert's $addToSet adds no tag it did not already carry.
                by_group[sorted(groups)[0]].append(Repository.model_validate(payload))
            for group, repos in by_group.items():
                PreparedStore(db, group).upsert_models("repositories", repos)
            print(f"\napplied to {sum(len(v) for v in by_group.values())} repositories "
                  f"across {len(by_group)} group(s)")
            if skipped:
                print(f"skipped {len(skipped)} repository(ies) belonging to no group; "
                      f"see {args.report}")
        else:
            print("\ndry run — nothing written; pass --apply")
        complete = True
    finally:
        client.close()
        # In the finally, not after it: under --apply the writes to Mongo are
        # already made by the time anything downstream can fail, and a run that
        # changed the database while leaving no record of what it changed is
        # the one case there is no recovering from. `complete` says whether the
        # walk finished, so a partial report cannot be read as a whole one.
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(
            {"created_at": datetime.now(UTC).isoformat(), "applied": args.apply,
             "complete": complete, "skipped_no_group": skipped, "changes": changes},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
