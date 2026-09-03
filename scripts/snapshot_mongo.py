"""Write prepared collections out as Extended JSON, one file per collection.

The rollback point for anything that rewrites prepared data, and at the same
time the input the rehearsal stand loads (`rehearsal_up.sh` runs mongoimport
over exactly these files). Extended JSON is what `bson.json_util` emits and
what mongoimport reads natively, so dates and ObjectIds survive the trip.

Reads only. Point it at a database with --uri; without one it uses the
configured connection, which for this checkout is the server.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

from pauk.settings import settings
from pauk.storage.mongo import get_mongo_client

# Everything the harvest chain writes: `repositories` gains contributors,
# `github_profiles` is rewritten wholesale, `persons` gains github/email/
# contributed_to. The rest are read-only to the harvest but the stand needs
# them — the repositories stage iterates `repo_links`, and a rehearsal that
# loads no publications, departments or organizations is not a rehearsal:
# every author it meets is created fresh instead of merged with.
#
# This is the list `rehearsal_up.sh` imports. Keep the two in step; the stand
# reads the manifest this script writes and refuses to start without a file.
DEFAULT = ("publications", "persons", "departments", "organizations",
           "repositories", "repo_links", "github_profiles")


def dump(db, name: str, out: Path, compress: bool) -> dict:
    path = out / (f"{name}.jsonl.gz" if compress else f"{name}.jsonl")
    digest = hashlib.sha256()
    count = 0
    with gzip.open(path, "wb") if compress else path.open("wb") as handle:
        for doc in db[name].find({}):
            line = (json_util.dumps(doc, ensure_ascii=False) + "\n").encode()
            digest.update(line)
            handle.write(line)
            count += 1
    return {"collection": name, "documents": count, "file": path.name,
            "bytes": path.stat().st_size, "sha256_of_content": digest.hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--uri", help="override the configured MongoDB connection")
    parser.add_argument("--db", default=settings.mongo_db)
    parser.add_argument("--collections", nargs="*", default=list(DEFAULT))
    parser.add_argument("--gzip", action="store_true", help="compress (mongoimport cannot read these)")
    args = parser.parse_args()

    client = MongoClient(args.uri) if args.uri else get_mongo_client(settings)
    try:
        db = client[args.db]
        args.out.mkdir(parents=True, exist_ok=True)
        report = [dump(db, name, args.out, args.gzip) for name in args.collections]
        # Read the address while the client is still open; it is unavailable
        # once closed, and the manifest is worthless without naming the source.
        host = client.address[0] if client.address else None
    finally:
        client.close()

    manifest = {"created_at": datetime.now(UTC).isoformat(), "database": args.db,
                "host": host, "collections": report}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for row in report:
        print(f"  {row['collection']:<18} {row['documents']:>7} docs  {row['bytes']/1e6:>8.1f} MB")
    print(f"manifest: {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
