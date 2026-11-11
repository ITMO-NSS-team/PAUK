"""Rebuild publications mistakenly folded together under different DOIs.

Dry run by default. With --apply, rebuild the affected records from their
own sources before replacing prepared rows and incident graph edges. Use
--from-report to repair records detached by the old migration or resume
an interrupted repair.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from pauk.graph.audit import actor_context, audited_client
from pauk.graph.client import resolve_folded_id
from pauk.graph.overrides import override_id, tombstoned_ids
from pauk.graph.publication_repair import apply_graph_repair, known_version_pairs
from pauk.graph.version_candidates import classify_pair
from pauk.jobs.locks import Busy, held
from pauk.jobs.models import GRAPH
from pauk.pipeline.publication_repair import (
    CannotRebuild,
    _work_sources,
    apply_prepared_changes,
    repair_pdf_writes,
    stage_repair,
)
from pauk.pipeline.stages.dedup import _norm_doi
from pauk.settings import settings
from pauk.storage import PreparedStore
from pauk.storage.atomic import AtomicWriter
from pauk.storage.mongo import get_mongo_client
from pauk.storage.pdf import PdfStore

logger = logging.getLogger(__name__)

OPENALEX = "https://openalex.org/"


def raw_work(db, work_id: str) -> tuple[dict, str] | None:
    """The newest raw OpenAlex payload of one work, and its group."""
    envelope = db.raw.find_one(
        {"source": "openalex_works", "payload.id": {"$in": [OPENALEX + work_id, work_id]}},
        sort=[("fetched_at", -1)])
    return (envelope["payload"], envelope["group"]) if envelope else None


def _row(work_id: str, entry: dict, payload: dict | None, fallback: dict | None = None) -> dict:
    """A record shaped for `classify_pair`, from its ledger entry and raw payload."""
    payload = payload or {}
    fallback = fallback or {}
    source = (payload.get("primary_location") or {}).get("source") or {}
    return {
        "id": work_id,
        "title": entry.get("title"),
        "doi": entry.get("doi"),
        "journal": entry.get("journal"),
        "venue_type": source.get("type") or fallback.get("venue_type"),
        "type": payload.get("type") or fallback.get("type"),
        "publication_date": entry.get("publication_date"),
        "year": entry.get("year"),
        "abstract": entry.get("abstract"),
        "authors": [{"person_id": author["person_id"]} for author in entry.get("authors") or []
                    if author and author.get("person_id")],
    }


def plan_splits(canonical: dict, versions: list[dict], merged_ids: list[str],
                raw_of) -> list[dict]:
    """The folded records of one survivor that are works of their own.

    Args:
        canonical: The survivor: id, doi, type, venue_type, title, ...
        versions: Its ledger, one dict per record, its own included.
        merged_ids: The ids folded into it.
        raw_of: work id -> (payload, group) or None.

    Returns:
        One dict per record to take apart: id, bucket, its lone authors
        (on it and not on the survivor) and the group its raw payload is in.
        Without a ledger entry of the survivor's own its authors are not
        known apart from the folded record's, so no authorship is taken
        away (`lone_authors` empty, `survivor_authors_known` False): a
        leftover authorship is less harm than a real one removed.
    """
    by_id = {entry.get("openalex_id"): entry for entry in versions}
    own = by_id.get(canonical["id"])
    authors_known = own is not None
    if own is None:
        # A graph row's authors are the union of both records: good enough
        # for the rules to see shared authors, not to tell who is lone.
        own = {key: canonical.get(key) for key in
               ("title", "doi", "journal", "publication_date", "year", "abstract", "authors")}
    survivor = _row(canonical["id"], own, None, canonical)
    survivor_doi = _norm_doi(canonical.get("doi") or own.get("doi"))
    splits = []
    for work_id in merged_ids:
        entry = by_id.get(work_id)
        if entry is None:
            continue
        doi = _norm_doi(entry.get("doi"))
        if not doi or doi == survivor_doi:
            continue
        raw = raw_of(work_id)
        if raw is None:
            logger.warning("split: %s (folded into %s) has no raw payload, left folded",
                           work_id, canonical["id"])
            continue
        result = classify_pair(survivor, _row(work_id, entry, raw[0]))
        if result is None:
            continue
        own_authors = {author["person_id"] for author in survivor["authors"]}
        survivor_raw = raw_of(canonical["id"])
        splits.append({
            "survivor": canonical["id"],
            "id": work_id,
            "bucket": result[0],
            "lone_authors": sorted({author["person_id"] for author in entry.get("authors") or []}
                                   - own_authors) if authors_known else [],
            "survivor_authors_known": authors_known,
            # Both are normalized again: the record is rebuilt in its own
            # group, and the survivor's gives back any authorship of it the
            # ledger did not know about and the repair took away.
            "groups": sorted({found[1] for found in (raw, survivor_raw) if found}),
        })
    return splits


def mongo_splits(db, raw_of) -> list[dict]:
    collection = db[PreparedStore.COLLECTIONS["publications"]]
    splits = []
    for doc in collection.find({"merged_ids.0": {"$exists": True}}):
        canonical = {"id": doc["_id"], **doc}
        splits += plan_splits(canonical, doc.get("versions") or [], doc.get("merged_ids") or [], raw_of)
    return splits


def graph_splits(client, raw_of) -> list[dict]:
    splits = []
    for row in client.fetch_publications_for_dedup(folded_only=True):
        if not row.get("merged_ids"):
            continue
        versions = row.get("versions") or []
        if isinstance(versions, str):
            versions = json.loads(versions)
        splits += plan_splits(row, versions, row["merged_ids"], raw_of)
    return splits


def _report_pairs(report):
    pairs = set()
    for field in ("mongo", "graph"):
        for row in report.get(field, []):
            pair = (row["survivor"], row["id"])
            if pair[0] == pair[1] or any(not re.fullmatch(r"W\d+", item) for item in pair):
                raise CannotRebuild(f"invalid split pair: {pair}")
            pairs.add(pair)
    returning = {}
    for survivor, work_id in sorted(pairs):
        if work_id in returning and returning[work_id] != survivor:
            raise CannotRebuild(f"conflicting survivors for {work_id}")
        returning[work_id] = survivor
    if returning.keys() & returning.values():
        raise CannotRebuild("the report names a survivor that is also being split off")
    return sorted(pairs)


def _validate_family(client, db, pairs, aliases=None):
    targets = {work_id for pair in pairs for work_id in pair}
    if targets & tombstoned_ids(db, "Publication"):
        raise CannotRebuild("a publication in this family was deleted by hand")
    returning = {work_id: survivor for survivor, work_id in pairs}
    for work_id in targets:
        parent = resolve_folded_id(client, "Publication", work_id)
        if parent not in {work_id, returning.get(work_id)}:
            raise CannotRebuild(f"{work_id} is now folded into {parent}")
    permitted = {**(aliases or {}), **returning}
    graph_rows = {row["id"]: row for row in client.fetch_publications_for_dedup(sorted(targets))}
    publications = db.publications.find({"$or": [
        {"_id": {"$in": sorted(targets)}}, {"merged_ids": {"$in": sorted(targets)}},
    ]})
    mongo_rows = {row["_id"]: row for row in publications}
    retained = {}
    for rows in (graph_rows, mongo_rows):
        for work_id, row in rows.items():
            for alias in row.get("merged_ids") or []:
                if alias in targets and returning.get(alias) != work_id:
                    raise CannotRebuild(f"{alias} now belongs to {work_id}")
                if alias in returning:
                    continue
                if work_id not in targets:
                    continue
                if alias in retained and retained[alias] != work_id:
                    raise CannotRebuild(f"conflicting retained fold for {alias}")
                if aliases is not None and permitted.get(alias) != work_id:
                    raise CannotRebuild(f"new fold into {work_id} since staging: {alias}")
                retained[alias] = work_id
    if aliases is not None and retained != aliases:
        raise CannotRebuild("retained folds changed since staging")
    for alias, survivor in retained.items():
        parent = resolve_folded_id(client, "Publication", alias)
        if parent not in {alias, survivor}:
            raise CannotRebuild(f"{alias} is now folded into {parent}")
    for work_id in targets:
        override = db.graph_overrides.find_one({
            "_id": override_id("Publication", work_id), "active": True,
        })
        if override:
            fields = override.get("fields") or {}
            keep = {alias for alias, parent in retained.items() if parent == work_id}
            if "merged_ids" in fields and set(fields["merged_ids"] or []) != keep:
                raise CannotRebuild(f"{work_id}: manual merged_ids conflict with this split")
            ledger = fields.get("versions") or []
            if isinstance(ledger, str):
                ledger = json.loads(ledger)
            if any(entry.get("openalex_id") not in keep | {work_id} for entry in ledger):
                raise CannotRebuild(f"{work_id}: manual version ledger conflicts with this split")
    return targets, retained


def _validate_sources(db, targets, pairs, aliases):
    works, groups = _work_sources(db, targets | aliases.keys())
    dois = {work_id: _norm_doi(work.get("doi")) for work_id, work in works.items()}
    for survivor, work_id in pairs:
        if not dois[survivor] or not dois[work_id] or dois[survivor] == dois[work_id]:
            raise CannotRebuild(f"{survivor}/{work_id} require different source DOIs")
    for alias, survivor in aliases.items():
        if dois[alias] and dois[survivor] and dois[alias] != dois[survivor]:
            raise CannotRebuild(f"{alias} has its own DOI and is still folded into {survivor}")
    return works, groups


def _stage_family(mongo, db, client, pairs):
    with held(db, GRAPH):
        targets, aliases = _validate_family(client, db, pairs)
        version_pairs, version_edges = known_version_pairs(client, db, targets | aliases.keys(), pairs)
    source_data = _validate_sources(db, targets, pairs, aliases)
    scratch = mongo[f"pauk_split_{uuid4().hex}"]
    folder = settings.pdf_dir / ".split-repair" / scratch.name
    folder.mkdir(mode=0o700, parents=True)
    try:
        stage_repair(db, scratch, settings, folder, targets, aliases, source_data)
        scratch.checkpoint.insert_one({
            "_id": "repair", "source_db": db.name, "pairs": [list(pair) for pair in pairs],
            "aliases": aliases, "version_pairs": [list(pair) for pair in version_pairs],
            "version_edges": [[src, tgt, props] for (src, tgt), props in version_edges.items()],
        })
        return scratch.name
    except BaseException:
        mongo.drop_database(scratch.name)
        shutil.rmtree(folder)
        raise


def _commit_family(mongo, db, client, pairs, state, save_report):
    name = state["scratch"]
    if not re.fullmatch(r"pauk_split_[a-f0-9]{32}", name):
        raise CannotRebuild("invalid staging database in the report")
    scratch = mongo[name]
    plan = scratch.checkpoint.find_one({
        "_id": "repair", "source_db": db.name, "pairs": [list(pair) for pair in pairs],
    })
    if plan is None:
        raise CannotRebuild(f"staging database {name} does not match this repair")
    aliases = plan["aliases"]
    folder = settings.pdf_dir / ".split-repair" / name
    with ExitStack() as locks, actor_context("split-publication-versions", source="script"):
        for group in sorted(scratch.changes.distinct("groups")):
            locks.enter_context(held(db, f"group:{group}"))
        locks.enter_context(held(db, GRAPH))
        targets, _ = _validate_family(client, db, pairs, aliases)
        pdfs = repair_pdf_writes(db, scratch, settings, folder, targets | aliases.keys())
        apply_prepared_changes(db, scratch)
        cache = PdfStore(db, settings.pdf_dir)
        for work_id, pdf in pdfs.items():
            cache.save(work_id, pdf)
        state["status"] = "prepared"
        save_report()
        current_pairs, current_edges = known_version_pairs(client, db, targets | aliases.keys(), pairs)
        version_pairs = sorted(set(map(tuple, plan["version_pairs"])) | set(current_pairs))
        history = {(src, tgt): props for src, tgt, props in plan["version_edges"]}
        history.update(current_edges)
        apply_graph_repair(client, db, scratch, targets, aliases, pairs, version_pairs, history)
    state["status"] = "done"
    state.pop("error", None)
    save_report()
    mongo.drop_database(name)
    if folder.exists():
        shutil.rmtree(folder)


def _save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with AtomicWriter(path) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)


def _families(report):
    families = defaultdict(list)
    for survivor, work_id in _report_pairs(report):
        families[survivor].append((survivor, work_id))
    return sorted(families.items())


def _repair(mongo, db, client, report, report_path):
    progress = report.setdefault("progress", {})
    failed = False
    for survivor, pairs in _families(report):
        state = progress.setdefault(survivor, {})
        if state.get("status") == "done":
            continue
        try:
            if "scratch" not in state:
                state["scratch"] = _stage_family(mongo, db, client, pairs)
                state["status"] = "staged"
                _save_report(report_path, report)
            _commit_family(mongo, db, client, pairs, state, lambda: _save_report(report_path, report))
        except Exception as error:
            failed = True
            state["error"] = str(error)
            logger.exception("repair of %s postponed", survivor)
            _save_report(report_path, report)
    return int(failed)


def _discover(db, client):
    cache = {}

    def raw_of(work_id):
        if work_id not in cache:
            cache[work_id] = raw_work(db, work_id)
        return cache[work_id]

    with held(db, GRAPH):
        return {"mongo": mongo_splits(db, raw_of), "graph": graph_splits(client, raw_of)}


def _unfinished_report(path, *, include_unstaged):
    if not path.exists():
        return None
    previous = json.loads(path.read_text(encoding="utf-8"))
    progress = previous.get("progress", {})
    if (include_unstaged or progress) and any(
        progress.get(survivor, {}).get("status") != "done" for survivor, _ in _families(previous)
    ):
        return previous
    return None


def _preview(db, client, report):
    errors = {}
    progress = report.get("progress", {})
    with held(db, GRAPH):
        for survivor, pairs in _families(report):
            if progress.get(survivor, {}).get("status") == "done":
                continue
            try:
                targets, aliases = _validate_family(client, db, pairs)
                _validate_sources(db, targets, pairs, aliases)
            except CannotRebuild as error:
                errors[survivor] = str(error)
                logger.warning("repair of %s postponed: %s", survivor, error)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="rebuild and apply (default: dry run)")
    parser.add_argument("--from-report", type=Path, help="repair or resume the pairs in this JSON report")
    parser.add_argument("--report", type=Path, help="output JSON report")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    source = args.from_report
    report_path = args.report or (
        source.with_name(source.stem + ".repair.json") if source
        else Path("data/reports/split-publication-versions.json")
    )
    if source and source.resolve() == report_path.resolve():
        parser.error("--report must differ from --from-report")

    mongo = get_mongo_client(settings)
    db = mongo[settings.mongo_db]
    client = audited_client(settings, db)
    try:
        if source:
            report = json.loads(source.read_text(encoding="utf-8"))
        else:
            report = _unfinished_report(report_path, include_unstaged=args.apply) or _discover(db, client)
        pairs = _report_pairs(report)
        if source and report_path.exists():
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            if _report_pairs(previous) == pairs and previous.get("source_db") == db.name:
                report = previous
            elif previous.get("progress") and _unfinished_report(report_path, include_unstaged=False):
                raise CannotRebuild("the output report holds a different unfinished repair")
        if report.get("source_db", db.name) != db.name:
            raise CannotRebuild("this report belongs to another source database")
        report["source_db"] = db.name
        print(f"{len(pairs)} publication pair(s) to rebuild. Report: {report_path}")
        if not args.apply:
            report["dry_run_errors"] = _preview(db, client, report)
        _save_report(report_path, report)
        return _repair(mongo, db, client, report, report_path) if args.apply else int(bool(report["dry_run_errors"]))
    except (Busy, CannotRebuild, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        client.close()
        mongo.close()


if __name__ == "__main__":
    sys.exit(main())
