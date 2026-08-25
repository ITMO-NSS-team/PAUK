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


def repo_id_from_url(url: str) -> str | None:
    parsed = urlparse((url or "").rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc.lower() not in GITHUB_HOSTS or len(parts) != 2:
        return None
    return f"github_{parts[0].lower()}_{parts[1].lower()}"


def irrelevant_claims(db) -> dict[str, set[str]]:
    """Per repository, the publications whose every link to it was judged False.

    A publication that links the same repository twice — once as a dependency
    and once as its own code — keeps the claim: one relevant link is enough.
    """
    verdicts: dict[str, dict[str, set[bool | None]]] = defaultdict(lambda: defaultdict(set))
    for row in db[PreparedStore.COLLECTIONS["repo_links"]].find({}, {"publication_id": 1, "links": 1}):
        publication = row.get("publication_id")
        if not publication:
            continue
        for link in row.get("links") or []:
            repo_id = repo_id_from_url(link.get("url") or "")
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

    client = get_mongo_client(settings)
    db = client[settings.mongo_db]
    try:
        drop = irrelevant_claims(db)
        collection = db[PreparedStore.COLLECTIONS["repositories"]]
        changes = []
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
                payload = {k: v for k, v in doc.items() if k not in ("_id", "groups", "_version")}
                payload["id"] = doc["_id"]
                payload["publication_ids"] = c["after"]
                # Written under a group the repository already belongs to, so
                # the upsert's $addToSet adds no tag it did not already carry.
                by_group[sorted(doc.get("groups") or ["__none__"])[0]].append(
                    Repository.model_validate(payload))
            for group, repos in by_group.items():
                PreparedStore(db, group).upsert_models("repositories", repos)
            print(f"\napplied to {sum(len(v) for v in by_group.values())} repositories "
                  f"across {len(by_group)} group(s)")
        else:
            print("\ndry run — nothing written; pass --apply")
    finally:
        client.close()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(
        {"created_at": datetime.now(UTC).isoformat(), "applied": args.apply, "changes": changes},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
