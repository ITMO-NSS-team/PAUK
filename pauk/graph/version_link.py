"""Write the version rules' pairs into the graph as IS_VERSION.

`pauk versions link`. Same pairs as `pauk versions report`
(`version_candidates.py`), but each one becomes an edge
`(:Publication)-[:IS_VERSION {bucket, ...}]->(:Publication)` instead of a
journal line only. Every bucket gets one, the look-alikes included, so the
graph says why two near-identical records are *not* one work as plainly as
why they are. Nothing is folded: a version keeps its own node, DOI and
authors.

The edges belong to this command, not to a publish (no prepared row carries
them), and each run rebuilds them whole. An edge the rules no longer produce
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
from .client import chunked
from .dedup import _ordinal
from .overrides import apply_overrides, claimed_relationships, tombstoned_relationships
from .version_candidates import CANDIDATES_FILENAME, find_version_candidates
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
        answer = answers.get(frozenset((a_id, b_id)))
        if answer == review.DIFFERENT:
            continue
        # A look-alike is explained, not asked about: nobody confirms that an
        # erratum is an erratum.
        status = CONFIRMED if answer == review.SAME else HELD if row["mergeable"] else None
        edges[edge_direction(by_id[a_id], by_id[b_id], row)] = {
            "bucket": row["bucket"], "mergeable": row["mergeable"], "status": status,
            **{name: row.get(name) for name in _EDGE_FIELDS},
        }
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


def _own(triples: set[tuple[str, str, str, str, str]]) -> set[tuple[str, str]]:
    """The IS_VERSION pairs among override triples."""
    return {(src_id, tgt_id) for src_label, rel_type, tgt_label, src_id, tgt_id in triples
            if (src_label, rel_type, tgt_label) == ("Publication", REL_TYPE, "Publication")}


def _link_locked(config: Settings, mongo_db: Database) -> dict[str, int]:
    client = audited_client(config, mongo_db)
    try:
        rows = client.fetch_publications_for_dedup()
        aliases = client.fetch_merged_id_map("Publication")
        answers = review.version_decisions(mongo_db, aliases)
        judge = VersionJudge(known=stored_verdicts(mongo_db, aliases))
        report = find_version_candidates(client, judge, rows)
        # An edge deleted by hand is left out rather than written and taken
        # away again by the overrides: that would log a creation and a
        # deletion on every run, and readers would see it in between.
        deleted = _own(tombstoned_relationships(mongo_db))
        deleted_pairs = {frozenset(pair) for pair in deleted}
        report = [row for row in report
                  if frozenset((row["record_a"], row["record_b"])) not in deleted_pairs]
        edges = plan_version_edges(rows, report, answers)

        existing = client.fetch_relationship_pairs("Publication", REL_TYPE, "Publication")
        # A pair a person called one work stays linked when the rules lose
        # it: a retitled record does not make the answer wrong.
        confirmed = {pair for pair in existing
                     if answers.get(frozenset(pair)) == review.SAME and pair not in deleted}
        stale = sorted(existing - set(edges) - _own(claimed_relationships(mongo_db)) - confirmed)
        written = removed = 0
        with actor_context("etl-pipeline", source="versions-graph"):
            for chunk in chunked(stale):
                removed += client.delete_relationships_batch("Publication", "Publication", REL_TYPE, chunk)
            for chunk in chunked([(src, tgt, props) for (src, tgt), props in sorted(edges.items())]):
                written += client.upsert_relationships_batch("Publication", "Publication", REL_TYPE, chunk)

        # Only what a person can usefully answer goes to the queue.
        held_rows = [row for row in report if row["mergeable"]]
        asked = review.record_held(mongo_db, held_rows, source=review.GRAPH)
        retired = review.retire_versions(mongo_db, held_rows)
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        with AtomicWriter(config.cache_dir / CANDIDATES_FILENAME) as fh:
            for row in report:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("versions: %d IS_VERSION edge(s) written, %d stale removed, %d question(s) "
                    "(%d retired)", written, removed, asked, retired)

        with actor_context("etl-pipeline", source="versions-graph"):
            overrides = apply_overrides(client, mongo_db)
        return {
            "version_edges": written,
            "version_edges_removed": removed,
            "version_questions": asked,
            "version_questions_retired": retired,
            **overrides,
        }
    finally:
        client.close()
