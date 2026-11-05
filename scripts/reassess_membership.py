"""Build a membership reassessment plan without writing to MongoDB or Neo4j."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

from pauk.models import Person
from pauk.pipeline.membership_rebuild import build_membership_source_index, rebuild_from_global_sources

COLLECTIONS = ("persons", "publications", "person_merge_sources", "raw")


def _preserve_source_metadata(original: list[dict], updated: list[dict], keys: tuple[str, ...]) -> list[dict]:
    rows = []
    for row in updated:
        matches = [old for old in original if all(old.get(key) == row.get(key) for key in keys)]
        rows.append({**matches[0], **row} if len(matches) == 1 else row)
    return rows


def write_plan(db, output: Path) -> dict:
    """Keep all original fields and groups; the output is a proposal, not an import."""
    output.mkdir(parents=True, exist_ok=False)
    source_index = build_membership_source_index(db.raw.find({}), db.publications.find({}))
    counts = Counter()
    with (output / "persons.jsonl").open("w", encoding="utf-8") as handle:
        for original in db.persons.find({}).sort("id", 1):
            person = Person.model_validate(original)
            result = rebuild_from_global_sources(db, person, source_index=source_index)
            updated = result.model_dump(mode="json", by_alias=True)
            # A migration of one derived property must not normalize unrelated fields.
            fields = {key: value for key, value in updated.items()
                      if key.startswith("membership_") or key in {"is_itmo", "affiliations", "authored"}}
            for field, keys in (
                ("authored", ("publication_id", "position", "institution_source_person_id", "institution_source_id")),
                ("affiliations", ("source", "source_id", "source_person_id", "name", "ror")),
            ):
                fields[field] = _preserve_source_metadata(original.get(field) or [], fields[field], keys)
            row = {**original, **fields}
            counts["persons"] += 1
            counts[result.membership_status or "unknown"] += 1
            counts["cleared"] += bool(person.is_itmo and not result.is_itmo)
            counts["promoted"] += bool(not person.is_itmo and result.is_itmo)
            handle.write(json_util.dumps(row, ensure_ascii=False) + "\n")
    report = dict(counts)
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot-dir", type=Path, help="complete local JSONL export of the four source collections")
    source.add_argument("--mongo-uri", help="explicit read-only MongoDB connection; never taken from .env")
    parser.add_argument("--db", help="required with --mongo-uri")
    parser.add_argument("--out", required=True, type=Path, help="new directory for a reviewable plan")
    args = parser.parse_args()
    if args.snapshot_dir:
        try:
            import mongomock
        except ImportError:
            parser.error("snapshot mode requires mongomock; run with uv run --with mongomock")
        paths = {name: args.snapshot_dir / f"{name}.jsonl" for name in COLLECTIONS}
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            parser.error("incomplete snapshot: " + ", ".join(missing))
        client = mongomock.MongoClient()
        db = client["membership_reassessment"]
        for name, path in paths.items():
            with path.open(encoding="utf-8-sig") as handle:
                for line in handle:
                    if line.strip():
                        db[name].insert_one(json_util.loads(line))
    else:
        if not args.db:
            parser.error("--db is required with --mongo-uri")
        client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10000)
        db = client[args.db]
    try:
        print(json.dumps(write_plan(db, args.out), ensure_ascii=False))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
