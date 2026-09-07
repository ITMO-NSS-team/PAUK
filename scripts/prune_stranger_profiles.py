"""Drop GitHub profiles harvested only from repositories nobody implements.

The harvest collects everyone credited with a commit on every repository a
paper links to, and papers link to the tools they used. Once IMPLEMENTS is
repaired, the repositories left with no claim at all are exactly those someone
else wrote, and the accounts reachable only through them are contributors to
third-party libraries who have no connection to the institute.

A profile is kept when any of these holds, so that nothing the graph relies on
loses its target:

* some Person was matched to it (`Person.github`);
* it owns a repository (the OWNED_BY edge points at it);
* any repository it appears on still has a publication claiming it.

`Repository.contributors` is left alone: who committed to a repository is a
fact about the repository, and it is a node property rather than an edge, so
it does not dangle when a profile goes.

Dry run by default.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pauk.settings import settings
from pauk.storage import PreparedStore
from pauk.storage.mongo import get_mongo_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument("--report", type=Path, default=Path("data/reports/prune-profiles.json"))
    args = parser.parse_args()

    client = get_mongo_client(settings)
    db = client[settings.mongo_db]
    try:
        repos = list(db[PreparedStore.COLLECTIONS["repositories"]].find(
            {}, {"url": 1, "publication_ids": 1, "owner_login": 1}))
        claimed_urls = {(r.get("url") or "").rstrip("/") for r in repos if r.get("publication_ids")}
        owners = {(r.get("owner_login") or "").lower() for r in repos if r.get("owner_login")}
        matched = {(p["github"] or "").lower()
                   for p in db[PreparedStore.COLLECTIONS["persons"]].find(
                       {"github": {"$nin": [None, ""]}}, {"github": 1})}

        profiles = list(db[PreparedStore.COLLECTIONS["github_profiles"]].find({}))
        doomed, kept_reason = [], {"matched": 0, "owner": 0, "on_a_claimed_repo": 0}
        for profile in profiles:
            login = (profile.get("login") or "").lower()
            if login in matched:
                kept_reason["matched"] += 1
                continue
            if login in owners:
                kept_reason["owner"] += 1
                continue
            if any((url or "").rstrip("/") in claimed_urls for url in profile.get("repos") or []):
                kept_reason["on_a_claimed_repo"] += 1
                continue
            doomed.append({"id": profile["_id"], "login": profile.get("login"),
                           "repos": profile.get("repos") or [], "groups": profile.get("groups")})

        print(f"profiles: {len(profiles)}")
        print(f"  kept, matched to a person:      {kept_reason['matched']}")
        print(f"  kept, owns a repository:        {kept_reason['owner']}")
        print(f"  kept, on a claimed repository:  {kept_reason['on_a_claimed_repo']}")
        print(f"  to delete:                      {len(doomed)}")
        print(f"  remaining after deletion:       {len(profiles) - len(doomed)}")

        if args.apply:
            ids = [d["id"] for d in doomed]
            for start in range(0, len(ids), 1000):
                db[PreparedStore.COLLECTIONS["github_profiles"]].delete_many(
                    {"_id": {"$in": ids[start:start + 1000]}})
            print(f"\ndeleted {len(ids)}; collection now holds "
                  f"{db[PreparedStore.COLLECTIONS['github_profiles']].count_documents({})}")
        else:
            print("\ndry run — nothing deleted; pass --apply")
    finally:
        client.close()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(
        {"created_at": datetime.now(UTC).isoformat(), "applied": args.apply, "deleted": doomed},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
