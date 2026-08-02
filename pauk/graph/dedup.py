"""Person deduplication across the whole graph.

The per-group dedup stage only sees one prepared group, but the graph
accumulates every published group: the same researcher collected in two
periods becomes two Person nodes that no group-level pass will ever compare
(each group's persons.jsonl holds only one of the ids). This module applies
the same merge rules (pauk/pipeline/stages/dedup.py) to all Person nodes at
once and folds duplicates directly in Neo4j.

The merged ids end up in the canonical node's `merged_ids` property, so a
later republish of an old group cannot resurrect a folded duplicate: the
loader re-folds any id found in a graph-side merged_ids map at the end of
every load (see jsonl_loader.load_jsonl_dir).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pauk.models import Person
from pauk.pipeline.normalize import _merge_person
from pauk.pipeline.stages.dedup import _union, plan_person_merges
from pauk.settings import Settings
from pauk.storage.atomic import AtomicWriter

from .client import Neo4jClient, chunked
from .extract import NODE_REGISTRY, extract_node

logger = logging.getLogger(__name__)

CANDIDATES_FILENAME = "dedup_candidates_graph.jsonl"


def collect_raw_orcids(raw_root: Path) -> dict[str, str | None]:
    """Trusted ORCIDs from every group's raw openalex_authors envelopes.

    Same reasoning as the per-group stage: the orcid property stored on a
    node may descend from the surname-only Crossref backfill, which can
    stamp a namesake's ORCID onto the wrong person. The author's own
    OpenAlex record is authoritative; a later fetch of the same author
    overrides an earlier one.
    """
    orcids: dict[str, str | None] = {}
    for path in sorted(raw_root.glob("*/openalex_authors.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line).get("payload") or {}
            except json.JSONDecodeError:
                continue  # torn final line of an interrupted fetch
            author_id = (payload.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
            if author_id:
                orcids[author_id] = (payload.get("orcid") or "").rstrip("/").rsplit("/", 1)[-1] or None
    return orcids


def dedup_graph_persons(client, raw_orcids: dict[str, str | None],
                        candidates_path: Path | None = None) -> dict[str, int]:
    """Fold duplicate Person nodes across all published groups.

    Args:
        client: An open Neo4jClient (or a compatible double).
        raw_orcids: Trusted ORCID per OpenAlex author id, from
            collect_raw_orcids().
        candidates_path: Where to write the review journal (applied merges
            marked "merged", held-back pairs marked "held"); None skips it.

    Returns:
        Counts of folded nodes and review candidates.
    """
    people = [
        Person(
            id=row["id"],
            openalex_id=row.get("openalex_id") or row["id"],
            is_itmo=bool(row.get("is_itmo")),
            name_en=row.get("name_en"),
            name_variants=list(row.get("name_variants") or []),
            orcid=row.get("orcid"),
            email=row.get("email"),
            github=row.get("github"),
            openreview=row.get("openreview"),
            google_scholar=row.get("google_scholar"),
            merged_ids=list(row.get("merged_ids") or []),
            authored=[
                {"publication_id": publication_id, "position": 0}
                for publication_id in row.get("publication_ids") or []
            ],
        )
        for row in client.fetch_persons_for_dedup()
    ]
    trusted_orcid = {
        person.id: raw_orcids[person.id] if person.id in raw_orcids else person.orcid
        for person in people
    }
    groups, report = plan_person_merges(people, trusted_orcid)

    merges: list[tuple[str, str]] = []
    canonical_nodes: dict[bool, list[tuple[str, dict]]] = {True: [], False: []}
    for canonical, duplicates in groups:
        for duplicate in duplicates:
            logger.info("graph dedup: merging %s (%s) into %s (%s)",
                        duplicate.id, duplicate.name_en, canonical.id, canonical.name_en)
            _merge_person(canonical, duplicate)
            if duplicate.name_en:
                canonical.name_variants = _union(canonical.name_variants, [duplicate.name_en])
            canonical.merged_ids = _union(canonical.merged_ids, [duplicate.id])
            merges.append((duplicate.id, canonical.id))
        # The canonical node inherits what its duplicates knew (variants,
        # filled scalars, merged_ids) before their nodes disappear.
        spec = NODE_REGISTRY["itmo_person" if canonical.is_itmo else "external_person"]
        _labels, node = extract_node(canonical.model_dump(by_alias=True, exclude_none=True), spec)
        canonical_nodes[canonical.is_itmo].append(node)

    for is_itmo, nodes in canonical_nodes.items():
        for chunk in chunked(nodes):
            client.upsert_person_nodes_batch(chunk, is_itmo)
    removed = 0
    for chunk in chunked(merges):
        removed += client.merge_person_nodes_batch(chunk)

    held = sum(1 for row in report if row["status"] == "held")
    if candidates_path is not None:
        with AtomicWriter(candidates_path) as fh:
            for row in report:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if report:
            logger.info("graph dedup: review journal in %s — %d merge(s) applied, %d pair(s) held",
                        candidates_path, removed, held)
    return {"graph_persons_merged": removed, "graph_dedup_candidates": held}


def run_graph_dedup(config: Settings) -> dict[str, int]:
    """CLI entry point for `pauk dedup graph`."""
    client = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
    try:
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        return dedup_graph_persons(
            client,
            collect_raw_orcids(config.raw_dir),
            candidates_path=config.cache_dir / CANDIDATES_FILENAME,
        )
    finally:
        client.close()
