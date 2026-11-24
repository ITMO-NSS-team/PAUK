"""Plan membership reassessment, then explicitly apply a reviewed plan to MongoDB."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

from pauk.models import Person
from pauk.pipeline.membership import POLICY_VERSION
from pauk.pipeline.membership_rebuild import build_membership_source_index, rebuild_from_global_sources

COLLECTIONS = ("persons", "publications", "person_merge_sources", "raw")
SOURCE_COLLECTIONS = COLLECTIONS[1:]
JOURNAL = "membership_reassessments"


def _digest(value: object) -> str:
    encoded = json_util.dumps(value, json_options=json_util.CANONICAL_JSON_OPTIONS, sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprints(db) -> dict[str, str]:
    return {name: _digest(sorted(_digest(row) for row in db[name].find({}))) for name in SOURCE_COLLECTIONS}


def _membership_field(key: str) -> bool:
    return key.startswith("membership_") or key in {"is_itmo", "affiliations", "authored"}


def _read_rows(path: Path) -> list[dict]:
    return [json_util.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _preserve_source_metadata(original: list[dict], updated: list[dict], keys: tuple[str, ...]) -> list[dict]:
    rows = []
    used = set()
    for row in updated:
        matches = [
            index
            for index, old in enumerate(original)
            if index not in used and all(old.get(key) == row.get(key) for key in keys)
        ]
        if keys == ("publication_id", "position"):
            # Rebuilding can add provenance to a legacy authorship; it must not
            # discard unrelated metadata or transfer it between known owners.
            owner_keys = ("institution_source_person_id", "institution_source_id")
            compatible = [
                index
                for index in matches
                if all(
                    not original[index].get(key) or not row.get(key) or original[index][key] == row[key]
                    for key in owner_keys
                )
            ]
            if matches and not compatible:
                raise ValueError("authorship owner changed: cannot preserve source metadata safely")
            matches = compatible
            identified = [
                index for index in matches if all(original[index].get(key) == row.get(key) for key in owner_keys)
            ]
            if identified:
                matches = identified
        exact = [
            index
            for index in matches
            if all(original[index][key] == row[key] for key in original[index].keys() & row.keys())
        ]
        if len(matches) > 1 and not exact:
            raise ValueError("ambiguous source rows: cannot preserve their metadata safely")
        if matches:
            index = exact[0] if exact else matches[0]
            used.add(index)
            row = {**original[index], **row}
        rows.append(row)
    return rows


def write_plan(db, output: Path) -> dict:
    """Keep all original fields and groups; the output is a proposal, not an import."""
    output.mkdir(parents=True, exist_ok=False)
    source_fingerprints = _fingerprints(db)
    originals = list(db.persons.find({}).sort("id", 1))
    source_index = build_membership_source_index(db.raw.find({}), db.publications.find({}))
    counts = Counter()
    with (output / "persons.jsonl").open("w", encoding="utf-8") as handle:
        for original in originals:
            person = Person.model_validate(original)
            result = rebuild_from_global_sources(db, person, source_index=source_index)
            updated = result.model_dump(mode="json", by_alias=True)
            # A migration of one derived property must not normalize unrelated fields.
            fields = {key: value for key, value in updated.items() if _membership_field(key)}
            for field, keys in (
                ("authored", ("publication_id", "position")),
                ("affiliations", ("source", "source_id", "source_person_id", "name", "ror")),
            ):
                fields[field] = _preserve_source_metadata(original.get(field) or [], fields[field], keys)
            row = {**original, **fields}
            counts["persons"] += 1
            counts[result.membership_status or "unknown"] += 1
            counts["cleared"] += bool(person.is_itmo and not result.is_itmo)
            counts["promoted"] += bool(not person.is_itmo and result.is_itmo)
            handle.write(json_util.dumps(row, ensure_ascii=False) + "\n")
    if _fingerprints(db) != source_fingerprints or sorted(_digest(row) for row in db.persons.find({})) != sorted(
        _digest(row) for row in originals
    ):
        raise ValueError(
            "database changed while planning; discard this incomplete plan and retry with writers stopped"
        )
    with (output / "originals.jsonl").open("w", encoding="utf-8") as handle:
        for row in originals:
            handle.write(json_util.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "format_version": 1,
        "policy_version": POLICY_VERSION,
        "persons_count": len(originals),
        "sources": source_fingerprints,
        "files": {
            name: sha256((output / name).read_bytes()).hexdigest() for name in ("persons.jsonl", "originals.jsonl")
        },
    }
    manifest["plan_id"] = _digest(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report = dict(counts)
    report["plan_id"] = manifest["plan_id"]
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _load_plan(directory: Path, plan_id: str) -> tuple[dict, list[tuple[dict, dict]]]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "plan_id"}
    if manifest.get("plan_id") != plan_id or _digest(unsigned) != plan_id:
        raise ValueError("plan ID does not match the reviewed manifest")
    if manifest.get("format_version") != 1 or manifest.get("policy_version") != POLICY_VERSION:
        raise ValueError("unsupported plan format or membership policy; regenerate the plan")
    if set(manifest.get("files", {})) != {"persons.jsonl", "originals.jsonl"}:
        raise ValueError("incomplete plan files")
    for name, checksum in manifest["files"].items():
        if sha256((directory / name).read_bytes()).hexdigest() != checksum:
            raise ValueError(f"plan file changed after review: {name}")
    originals, proposals = (_read_rows(directory / name) for name in ("originals.jsonl", "persons.jsonl"))
    if len(originals) != manifest["persons_count"] or len(proposals) != len(originals):
        raise ValueError("plan row count mismatch")
    pairs = list(zip(originals, proposals, strict=True))
    seen = set()
    seen_ids = set()
    for original, proposal in pairs:
        if "_id" not in original or not original.get("id"):
            raise ValueError("plan requires each original Mongo _id and person id")
        key = _digest(original["_id"])
        if key in seen or original["id"] in seen_ids:
            raise ValueError("duplicate person in plan")
        seen.add(key)
        seen_ids.add(original["id"])
        if {k: v for k, v in original.items() if not _membership_field(k)} != {
            k: v for k, v in proposal.items() if not _membership_field(k)
        }:
            raise ValueError("plan changes unrelated person fields")
        Person.model_validate(proposal)
        if type(original.get("_version", 0)) is not int or original.get("_version", 0) < 0:
            raise ValueError("invalid original person version")
    return manifest, pairs


def _target(original: dict, proposal: dict) -> dict:
    if _digest(original) == _digest(proposal):
        return original
    return {**proposal, "_version": original.get("_version", 0) + 1}


def _journal_id(plan_id: str, original: dict) -> str:
    return f"{plan_id}:{_digest(original['_id'])}"


def _validate_current(db, pairs: list[tuple[dict, dict]], plan_id: str) -> None:
    current = {_digest(row["_id"]): row for row in db.persons.find({})}
    if set(current) != {_digest(original["_id"]) for original, _ in pairs}:
        raise ValueError("person inventory changed since planning")
    for original, proposal in pairs:
        row = current[_digest(original["_id"])]
        if _digest(row) == _digest(original):
            continue
        target = _target(original, proposal)
        journal = db[JOURNAL].find_one({"_id": _journal_id(plan_id, original)})
        if (
            _digest(row) != _digest(target)
            or not journal
            or journal.get("before_hash") != _digest(original)
            or journal.get("after_hash") != _digest(target)
        ):
            raise ValueError(f"person changed since planning: {original['id']}")


def _finish_entry(db, entry: dict) -> None:
    before = entry["snapshot"]
    db.revisions.update_one(
        {"_id": entry["_id"]},
        {
            "$setOnInsert": {
                "entity_type": "persons",
                "entity_id": before["id"],
                "version": before.get("_version", 0),
                "snapshot": before,
                "replaced_by_group": "membership-reassessment",
                "replaced_at": entry["started_at"],
                "membership_plan_id": entry["plan_id"],
            }
        },
        upsert=True,
    )
    db[JOURNAL].update_one({"_id": entry["_id"]}, {"$set": {"state": "applied"}})


def apply_plan(db, directory: Path, plan_id: str, *, apply: bool = False) -> dict:
    """Validate every row first; apply only reviewed fields with resumable backups.

    Stop collectors, enrichment, merges and publication while planning/applying.
    Standalone Mongo cannot provide a cross-collection snapshot transaction.
    """
    manifest, pairs = _load_plan(directory, plan_id)
    if _fingerprints(db) != manifest["sources"]:
        raise ValueError("source collections changed since planning")
    _validate_current(db, pairs, plan_id)
    changed = [(before, after) for before, after in pairs if _digest(before) != _digest(after)]
    report = {"plan_id": plan_id, "changed": len(changed), "applied": 0, "already_applied": 0, "dry_run": not apply}
    if not apply:
        return report
    if _fingerprints(db) != manifest["sources"]:
        raise ValueError("source collections changed during preflight")
    for before, proposal in changed:
        target = _target(before, proposal)
        entry_id = _journal_id(plan_id, before)
        entry = {
            "_id": entry_id,
            "plan_id": plan_id,
            "person_id": before["id"],
            "snapshot": before,
            "before_hash": _digest(before),
            "after_hash": _digest(target),
            "started_at": datetime.now(UTC).isoformat(),
            "state": "pending",
        }
        db[JOURNAL].update_one({"_id": entry_id}, {"$setOnInsert": entry}, upsert=True)
        entry = db[JOURNAL].find_one({"_id": entry_id})
        if entry["before_hash"] != _digest(before) or entry["after_hash"] != _digest(target):
            raise ValueError("conflicting reassessment journal entry")
        current = db.persons.find_one({"_id": before["_id"]})
        if _digest(current) == _digest(target):
            _finish_entry(db, entry)
            report["already_applied"] += 1
            continue
        # Compare the whole document, including newly added fields, to avoid lost enrichment.
        query = {"_id": before["_id"], "$expr": {"$eq": ["$$ROOT", {"$literal": before}]}}
        fields = {key: value for key, value in target.items() if _membership_field(key) or key == "_version"}
        removed = {key: "" for key in before if _membership_field(key) and key not in target}
        update = {"$set": fields}
        if removed:
            update["$unset"] = removed
        result = db.persons.update_one(query, update)
        if result.matched_count != 1:
            raise ValueError(f"person changed during apply: {before['id']}; earlier rows remain journalled")
        _finish_entry(db, entry)
        report["applied"] += 1
    if _fingerprints(db) != manifest["sources"]:
        raise ValueError("sources changed during apply; do not publish; regenerate and review the plan")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot-dir", type=Path, help="complete local JSONL export of the four source collections")
    source.add_argument("--mongo-uri", help="explicit MongoDB connection; never taken from .env")
    parser.add_argument("--db", help="required with --mongo-uri")
    parser.add_argument("--out", type=Path, help="new directory for a reviewable plan")
    parser.add_argument("--apply-plan", type=Path, help="validate an existing reviewed plan against MongoDB")
    parser.add_argument("--plan-id", help="plan_id copied from the reviewed summary")
    parser.add_argument("--apply", action="store_true", help="write the validated plan; otherwise check only")
    args = parser.parse_args()
    if args.apply_plan:
        if not args.mongo_uri or not args.db or args.out or not args.plan_id:
            parser.error("--apply-plan requires --mongo-uri, --db and --plan-id, and cannot use --out")
    elif not args.out or args.apply or args.plan_id:
        parser.error("planning requires --out; --apply and --plan-id require --apply-plan")
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
        report = (
            apply_plan(db, args.apply_plan, args.plan_id, apply=args.apply)
            if args.apply_plan
            else write_plan(db, args.out)
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
