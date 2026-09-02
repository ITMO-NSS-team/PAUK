"""Orchestration for `pauk dedup departments`.

Runs the funnel — normalize, block, score, band, LLM-adjudicate — turns the
accepted pairs into merge groups (union-find, then a per-group conflict
guard), folds every group into one canonical Department node in Neo4j and
writes a decision journal.

Nothing here invents new node ids: one existing node of each group is chosen
as canonical and the rest are folded into it, exactly like the person /
publication / repository passes in pauk/graph/dedup.py. Merged-away ids land
in `canonical.merged_ids`, so a later publish of an old group re-folds them.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from pymongo.database import Database

from pauk.pipeline.stages.dedup import _grouped
from pauk.settings import Settings
from pauk.sources import OpenRouterClient
from pauk.storage.atomic import AtomicWriter

from ..client import Neo4jClient, chunked
from .adjudicate import Adjudicator, Verdict
from .embeddings import load_embedder
from .matching import (
    AUTO_MERGE,
    KIND_CLASS,
    LLM,
    DepartmentRecord,
    PairSignals,
    assign_band,
    block,
    score_pair,
)

logger = logging.getLogger(__name__)

JOURNAL_FILENAME = "dedup_candidates_departments.jsonl"


def _records(client: Neo4jClient) -> list[DepartmentRecord]:
    return [
        DepartmentRecord(
            id=row["id"],
            name_en=row.get("name_en"),
            name_ru=row.get("name_ru"),
            name_variants=tuple(row.get("name_variants") or []),
            kind=row.get("kind"),
            parent_id=row.get("parent_id"),
            staff_ids=frozenset(row.get("staff_ids") or []),
            publication_ids=frozenset(row.get("publication_ids") or []),
        )
        for row in client.fetch_departments_for_dedup()
    ]


def _semantic_pairs(records: list[DepartmentRecord], embedder_name: str) -> set[tuple[str, str]]:
    embedder = load_embedder(embedder_name)
    if embedder is None:
        return set()
    return embedder.semantic_pairs({rec.id: rec.names for rec in records})


def _canonical(members: list[DepartmentRecord]) -> DepartmentRecord:
    """The group's surviving node: most complete, then most connected, then
    the smallest id for a stable choice across runs."""
    return max(members, key=lambda r: (
        bool(r.name_en) + bool(r.name_ru),
        len(r.staff_ids) + len(r.publication_ids),
        -len(r.id),
        r.id,
    ))


def _group_conflict(members: list[DepartmentRecord],
                    different: set[frozenset]) -> str | None:
    classes = {KIND_CLASS.get(m.kind) for m in members if m.kind} - {None}
    if len(classes) > 1:
        return f"group spans kind classes {sorted(classes)}"
    ids = {m.id for m in members}
    for pair in different:
        if pair <= ids:
            a, b = sorted(pair)
            return f"LLM ruled {a} and {b} distinct"
    return None


def run_department_dedup(config: Settings, mongo_db: Database, *,
                         dry_run: bool = False, embedder: str = "") -> dict[str, int]:
    client = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
    try:
        return _run(client, config, mongo_db, dry_run=dry_run, embedder=embedder)
    finally:
        client.close()


def _run(client: Neo4jClient, config: Settings, mongo_db: Database, *,
         dry_run: bool, embedder: str = "") -> dict[str, int]:
    records = _records(client)
    by_id = {rec.id: rec for rec in records}
    logger.info("dept dedup: %d Department node(s)", len(records))

    candidates = block(records, _semantic_pairs(records, embedder))
    scored: dict[frozenset, PairSignals] = {}
    banded: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for a, b in candidates:
        sig = score_pair(by_id[a], by_id[b])
        scored[frozenset((a, b))] = sig
        banded[assign_band(sig)].append((a, b))
    logger.info("dept dedup: candidates=%d  auto-merge=%d  llm=%d  auto-reject=%d",
                len(candidates), len(banded[AUTO_MERGE]), len(banded[LLM]), len(banded["auto-reject"]))

    merge_pairs: list[tuple[str, str]] = list(banded[AUTO_MERGE])
    pair_rule: dict[frozenset, str] = {frozenset(p): "auto-merge" for p in banded[AUTO_MERGE]}
    different: set[frozenset] = set()
    part_of: list[dict] = []
    verdicts: dict[frozenset, Verdict] = {}

    if banded[LLM]:
        adjudicator = _adjudicator(config, mongo_db)
        if adjudicator is None:
            logger.warning("dept dedup: no OPENROUTER_API_KEY — %d llm-band pair(s) held", len(banded[LLM]))
        for a, b in banded[LLM]:
            key = frozenset((a, b))
            verdict = adjudicator.verdict(by_id[a], by_id[b], scored[key]) if adjudicator else Verdict("unknown", 0.0, "")
            verdicts[key] = verdict
            if verdict.is_merge:
                merge_pairs.append((a, b))
                pair_rule[key] = f"llm:{verdict.relation}"
            elif verdict.relation == "parent_child":
                part_of.append({"a": a, "b": b, "reason": verdict.reason})
            elif verdict.relation in ("sibling", "unrelated"):
                different.add(key)
        if adjudicator:
            logger.info("dept dedup: llm calls=%d cache hits=%d", adjudicator.calls, adjudicator.cache_hits)

    journal: list[dict] = []
    merges: list[tuple[str, str]] = []
    for member_ids in _grouped(merge_pairs):
        members = [by_id[i] for i in member_ids]
        conflict = _group_conflict(members, different)
        if conflict:
            journal.append({"status": "held", "departments": sorted(member_ids), "held_because": conflict})
            continue
        canonical = _canonical(members)
        for dup in members:
            if dup.id == canonical.id:
                continue
            merges.append((dup.id, canonical.id))
            rule = pair_rule.get(frozenset((dup.id, canonical.id)), "transitive")
            journal.append({
                "status": "merged", "department_a": dup.id, "name_a": dup.name_en or dup.name_ru,
                "department_b": canonical.id, "name_b": canonical.name_en or canonical.name_ru,
                "merged_into": canonical.id, "rule": rule,
            })
    for suggestion in part_of:
        journal.append({"status": "part_of_suggested", **suggestion})
    for key, verdict in verdicts.items():
        if verdict.relation in ("sibling", "unrelated", "unknown"):
            a, b = sorted(key)
            journal.append({"status": "held", "department_a": a, "department_b": b,
                            "held_because": f"llm:{verdict.relation}", "reason": verdict.reason})

    _write_journal(config, journal)

    applied = 0
    if merges and not dry_run:
        _apply(client, by_id, merges)
        applied = len(merges)
    elif merges:
        logger.info("dept dedup: dry run — %d merge(s) computed, not applied", len(merges))

    return {
        "departments": len(records),
        "candidate_pairs": len(candidates),
        "auto_merges": len(banded[AUTO_MERGE]),
        "llm_pairs": len(banded[LLM]),
        "merges_applied": applied,
        "part_of_suggested": len(part_of),
        "held": sum(1 for row in journal if row["status"] == "held"),
    }


def _adjudicator(config: Settings, mongo_db: Database) -> Adjudicator | None:
    if not config.openrouter_api_key:
        return None
    client = OpenRouterClient(
        config.request_timeout, config.openrouter_api_key,
        config.llm_model, config.openrouter_proxy_url,
    )
    return Adjudicator(mongo_db, client)


def _apply(client: Neo4jClient, by_id: dict[str, DepartmentRecord], merges: list[tuple[str, str]]) -> None:
    """Carry each duplicate's spellings onto its canonical node, then fold."""
    extra_names: dict[str, list[str]] = defaultdict(list)
    for dup_id, canonical_id in merges:
        dup = by_id[dup_id]
        extra_names[canonical_id].extend(
            name for name in (dup.name_en, dup.name_ru, *dup.name_variants) if name
        )
    updates: list[tuple[str, dict]] = []
    for canonical_id, names in extra_names.items():
        canonical = by_id[canonical_id]
        keep = {canonical.name_en, canonical.name_ru}
        variants = list(dict.fromkeys([*canonical.name_variants, *names]))
        updates.append((canonical_id, {"name_variants": [v for v in variants if v and v not in keep]}))
    for batch in chunked(updates):
        client.upsert_nodes_batch("Department", batch)
    for batch in chunked(merges):
        client.merge_department_nodes_batch(batch)


def _write_journal(config: Settings, rows: list[dict]) -> None:
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    path = config.cache_dir / JOURNAL_FILENAME
    with AtomicWriter(path) as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    merged = sum(1 for row in rows if row["status"] == "merged")
    held = sum(1 for row in rows if row["status"] == "held")
    logger.info("dept dedup: journal %s — %d merged, %d held", path, merged, held)


__all__ = ["run_department_dedup"]
