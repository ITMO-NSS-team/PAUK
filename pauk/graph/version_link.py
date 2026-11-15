"""Write the version rules' pairs into the graph as IS_VERSION.

`pauk versions link`. Same pairs as `pauk versions report`
(`version_candidates.py`), but each one becomes an edge
`(:Publication)-[:IS_VERSION {bucket, ...}]->(:Publication)` instead of a
journal line only. Every bucket gets one, the look-alikes included, so the
graph says why two near-identical records are *not* one work as plainly as
why they are. Nothing is folded: a version keeps its own node, DOI and
authors.

The command reconciles these graph-owned edges with the latest rules and
review decisions. A review action can reconcile one pair directly. An edge the rules no longer produce
is removed, unless somebody added it by hand; one somebody deleted by hand
is not written again. A person's answer in the review queue outranks the
rules: "different" removes the edge for good, "same" marks it confirmed and
keeps it after the rules stop finding the pair.
"""

from __future__ import annotations

import json
import logging

from pymongo.database import Database

from pauk.jobs.locks import held
from pauk.jobs.models import GRAPH
from pauk.pipeline.stages.dedup import _publication_rank_key
from pauk.settings import Settings
from pauk.sources import OpenRouterClient
from pauk.storage import LlmLogStore, review
from pauk.storage.atomic import AtomicWriter

from .audit import actor_context, audited_client
from .client import chunked, resolve_folded_id
from .dedup import _ordinal
from .overrides import DELETE, LINK, apply_overrides, relationship_override_id
from .version_candidates import (
    CANDIDATES_FILENAME,
    MEDIUM_FUZZY,
    MERGE_BUCKETS,
    classify_pair,
    find_version_candidates,
    route_medium_fuzzy,
)
from .version_judge import LOG_COLLECTION, VersionJudge, stored_verdicts

logger = logging.getLogger(__name__)

REL_TYPE = "IS_VERSION"
HELD = "held"
CONFIRMED = "same"

_EDGE_FIELDS = ("title_sim", "llm_verdict", "llm_reason")


def edge_direction(a: dict, b: dict, row: dict) -> tuple[str, str]:
    """(version, work it is a version of).

    A notice or a cover points at the article it is about. Otherwise the
    record the merge rules would have kept as the face of the work is the
    target: the journal article over the preprint, the newer over the older.
    """
    for source, target in (("erratum", "original"), ("cover", "article")):
        if row.get(source):
            return row[source], row[target]
    first, second = sorted((a, b), key=lambda pub: _publication_rank_key(
        pub.get("type"), _ordinal(pub.get("publication_date")),
        pub.get("author_count") or 0, pub["id"]))
    return second["id"], first["id"]


def plan_version_edges(rows: list[dict], report: list[dict],
                       answers: dict[frozenset[str], str]) -> dict[tuple[str, str], dict]:
    """The IS_VERSION edges the graph should hold, with their properties.

    Args:
        rows: Publications as `fetch_publications_for_dedup` returns them.
        report: `find_version_candidates` over the same rows.
        answers: `review.version_decisions`, keyed by the two ids.

    Returns:
        (source id, target id) to the edge's properties. A None property is
        written as a removal, so a verdict that went away does not linger.
    """
    by_id = {row["id"]: row for row in rows}
    edges: dict[tuple[str, str], dict] = {}
    for row in report:
        a_id, b_id = row["record_a"], row["record_b"]
        verdict = answers.get(frozenset((a_id, b_id)))
        answer = {"verdict": verdict} if verdict else None
        edges.update(_desired_pair([by_id[a_id], by_id[b_id]], {}, row, answer, None, False))
    return edges


def run_version_link(config: Settings, mongo_db: Database, llm: bool = False,
                     force: bool = False) -> dict[str, int]:
    """Entry point for `pauk versions link`.

    Holds the graph like `pauk dedup graph`: a publish folding a node
    while this run links it would leave the edge on a node about to go.
    The model is asked before that, without the lock: hundreds of pairs
    one after another take long enough to make every publish in between
    fail with Busy. The verdicts are stored as they come, so the locked
    pass reads them like any earlier run's.

    Args:
        llm: Ask the model about MEDIUM_FUZZY pairs no earlier run asked
            about. Without it the verdicts already given still route.
        force: Ask again even where a verdict is already known.

    Raises:
        Busy: Something else is already writing the graph.
    """
    asked = failed = 0
    if llm:
        asked, failed = _ask_llm(config, mongo_db, force)
    with held(mongo_db, GRAPH):
        return {**_link_locked(config, mongo_db),
                "version_llm_calls": asked, "version_llm_failed": failed}


def _ask_llm(config: Settings, mongo_db: Database, force: bool) -> tuple[int, int]:
    """Ask the model about the MEDIUM_FUZZY pairs it has no verdict for.

    Reads the graph only; every verdict lands in `VERDICTS_COLLECTION`.

    Returns:
        (calls made, calls that gave no verdict).
    """
    client = audited_client(config, mongo_db)
    try:
        judge = VersionJudge(
            known=stored_verdicts(mongo_db, client.fetch_merged_id_map("Publication")),
            client=OpenRouterClient(config.request_timeout, config.openrouter_api_key,
                                    config.llm_model, config.openrouter_proxy_url),
            log=LlmLogStore(mongo_db, LOG_COLLECTION),
            force=force,
            store=mongo_db,
        )
        find_version_candidates(client, judge, client.fetch_publications_for_dedup())
        return judge.asked, judge.failed
    finally:
        client.close()


def _version_overrides(db, aliases, scope=None):
    query = {"active": True, "kind": "rel", "rel_type": REL_TYPE,
             "src_label": "Publication", "tgt_label": "Publication"}
    if scope is not None:
        left, right = scope
        query["_id"] = {"$in": [
            relationship_override_id("Publication", REL_TYPE, "Publication", src, tgt)
            for a in left for b in right for src, tgt in ((a, b), (b, a))
        ]}
    claims = {}
    deleted = set()
    for row in db.graph_overrides.find(query).sort([("updated_at", 1), ("_id", 1)]):
        direction = tuple(aliases.get(row[key], row[key]) for key in ("src_id", "target_id"))
        pair = frozenset(direction)
        if len(pair) == 2:
            if row["op"] == DELETE:
                deleted.add(pair)
            elif row["op"] == LINK:
                claims[pair] = direction
    return claims, deleted


def _candidate(a, b, judge):
    result = classify_pair(a, b)
    if result is None:
        return None
    bucket, signals = result
    if bucket == MEDIUM_FUZZY:
        bucket = route_medium_fuzzy(a, b, signals, judge)
    return {"status": HELD, "entity": "publication", "bucket": bucket,
            "mergeable": bucket in MERGE_BUCKETS, **signals}


def _desired_pair(rows, existing, candidate, answer, claim, deleted, *,
                  reopen=False, previous=None, restore_claim=False):
    if deleted or (answer and answer["verdict"] == review.DIFFERENT):
        return {}
    confirmed = answer is not None and answer["verdict"] == review.SAME
    if len(rows) != 2:
        return {}
    if candidate:
        direction = claim or edge_direction(*rows, candidate)
        status = HELD if candidate["mergeable"] or (reopen and existing) else None
        return {direction: {
            "bucket": candidate["bucket"], "mergeable": candidate["mergeable"],
            "status": CONFIRMED if confirmed else status,
            **{field: candidate.get(field) for field in _EDGE_FIELDS},
        }}
    if confirmed:
        prior = existing or previous
        preferred = edge_direction(*rows, {})
        if prior:
            direction = claim or (preferred if preferred in prior else min(prior))
            if direction in existing:
                return {direction: {"status": CONFIRMED}}
            props = prior.get(direction) or next(iter(prior.values()))
            return {direction: {
                **{key: props[key] for key in ("bucket", "mergeable", *_EDGE_FIELDS) if key in props},
                "status": CONFIRMED,
            }}
        evidence = answer.get("evidence") or {}
        if evidence.get("bucket") in MERGE_BUCKETS:
            return {claim or preferred: {
                "bucket": evidence["bucket"], "mergeable": True, "status": CONFIRMED,
                **{field: evidence.get(field) for field in _EDGE_FIELDS},
            }}
    if reopen and existing:
        preferred = edge_direction(*rows, {})
        direction = claim if claim in existing else preferred if preferred in existing else min(existing)
        return {direction: {"status": HELD}}
    if claim in existing:
        return {claim: {"status": None}}
    if restore_claim and previous and claim in previous:
        return {claim: {**previous[claim], "status": None}}
    return {}


def _diff(existing, desired):
    stale = sorted(existing.keys() - desired.keys())
    updates = [(src, tgt, props) for (src, tgt), props in sorted(desired.items())
               if (src, tgt) not in existing
               or any(existing[(src, tgt)].get(key) != value for key, value in props.items())]
    return stale, updates


def _write_diff(client, stale, updates):
    written = sum(client.upsert_relationships_batch("Publication", "Publication", REL_TYPE, chunk)
                  for chunk in chunked(updates))
    removed = sum(client.delete_relationships_batch("Publication", "Publication", REL_TYPE, chunk)
                  for chunk in chunked(stale))
    return written, removed


def reconcile_version_pair(client, db, members, *, reopen=False, record_question=False, previous=None):
    """Reconcile one pair while the caller holds GRAPH."""
    resolved = [resolve_folded_id(client, "Publication", member) for member in members]
    if resolved[0] == resolved[1]:
        return "version_folded", 0, 0

    ids = tuple(sorted(resolved))
    rows = sorted(client.fetch_publications_for_dedup(list(ids)), key=lambda row: row["id"])
    if len(rows) != 2:
        return "version_missing", 0, 0
    aliases = dict(zip(members, resolved, strict=True))
    for row in rows:
        aliases.update(dict.fromkeys(row.get("merged_ids") or [], row["id"]))
    scope = tuple({work_id, *(alias for alias, target in aliases.items() if target == work_id)}
                  for work_id in ids)
    pair = frozenset(ids)
    answer = review.version_answers(db, aliases, scope).get(pair)
    claims, deleted = _version_overrides(db, aliases, scope)
    existing = client.fetch_version_edges(ids)
    candidate = None
    if pair not in deleted and (answer is None or answer["verdict"] != review.DIFFERENT):
        candidate = _candidate(*rows, VersionJudge(known=stored_verdicts(db, aliases, scope)))
    desired = _desired_pair(rows, existing, candidate, answer, claims.get(pair), pair in deleted,
                            reopen=reopen, previous=previous, restore_claim=record_question)
    written, removed = _write_diff(client, *_diff(existing, desired))
    if record_question:
        held_rows = [candidate] if candidate and candidate["mergeable"] and answer is None else []
        review.record_held(db, held_rows, source=review.GRAPH)
        review.retire_versions(db, held_rows, scope=scope)
    if pair in deleted:
        done = "no_edge_deleted"
    elif answer and answer["verdict"] == review.DIFFERENT:
        done = "unlinked" if removed else "already_unlinked"
    elif answer:
        done = "linked" if desired else "no_edge"
    else:
        done = "reopened" if desired else "relink"
    return done, written, removed


def run_version_pair(config: Settings, db: Database, members: tuple[str, str]) -> dict[str, int]:
    with held(db, GRAPH), actor_context("etl-pipeline", source="versions-review"):
        client = audited_client(config, db)
        try:
            _, written, removed = reconcile_version_pair(client, db, members, reopen=True)
            return {"version_edges": written, "version_edges_removed": removed}
        finally:
            client.close()


def _link_locked(config: Settings, mongo_db: Database) -> dict[str, int]:
    client = audited_client(config, mongo_db)
    try:
        rows = client.fetch_publications_for_dedup()
        by_id = {row["id"]: row for row in rows}
        aliases = client.fetch_merged_id_map("Publication")
        answers = review.version_answers(mongo_db, aliases)
        judge = VersionJudge(known=stored_verdicts(mongo_db, aliases))
        report = find_version_candidates(client, judge, rows)
        claims, deleted = _version_overrides(mongo_db, aliases)
        candidates = {frozenset((row["record_a"], row["record_b"])): row for row in report}
        previous = {}
        for direction, props in client.fetch_version_edges().items():
            previous.setdefault(frozenset(direction), {})[direction] = props

        stale = []
        updates = []
        for pair in sorted(previous.keys() | candidates.keys() | answers.keys(), key=sorted):
            existing = previous.get(pair, {})
            desired = _desired_pair([by_id[work_id] for work_id in sorted(pair) if work_id in by_id],
                                    existing, candidates.get(pair), answers.get(pair),
                                    claims.get(pair), pair in deleted)
            removals, changes = _diff(existing, desired)
            stale.extend(removals)
            updates.extend(changes)

        report = [row for pair, row in candidates.items() if pair not in deleted]
        with actor_context("etl-pipeline", source="versions-graph"):
            written, removed = _write_diff(client, stale, updates)
            overrides = apply_overrides(client, mongo_db)
            fresh = review.version_answers(mongo_db, aliases)
            late = sorted((pair for pair in answers.keys() | fresh.keys()
                           if answers.get(pair) != fresh.get(pair)), key=sorted)
            for pair in late:
                existing = client.fetch_version_edges(tuple(sorted(pair)))
                desired = _desired_pair(
                    [by_id[work_id] for work_id in sorted(pair) if work_id in by_id],
                    existing, candidates.get(pair), fresh.get(pair), claims.get(pair),
                    pair in deleted, reopen=fresh.get(pair) is None, previous=previous.get(pair),
                )
                added, dropped = _write_diff(client, *_diff(existing, desired))
                written += added
                removed += dropped

        held_rows = [row for pair, row in candidates.items()
                     if pair not in deleted and pair not in fresh and row["mergeable"]]
        asked = review.record_held(mongo_db, held_rows, source=review.GRAPH)
        retired = review.retire_versions(mongo_db, held_rows)
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        with AtomicWriter(config.cache_dir / CANDIDATES_FILENAME) as fh:
            for row in report:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return {
            "version_edges": written,
            "version_edges_removed": removed,
            "version_questions": asked,
            "version_questions_retired": retired,
            "version_late_answers": len(late),
            **overrides,
        }
    finally:
        client.close()
