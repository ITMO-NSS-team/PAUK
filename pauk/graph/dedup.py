"""Deduplication across the whole graph, not one prepared group.

The per-group dedup stage only sees one prepared group, but the graph
accumulates every published group: the same researcher (or work, or
repository) collected in two periods becomes two nodes that no group-level
pass will ever compare — each group's JSONL holds only one of the ids.
This module applies the same merge rules (pauk/pipeline/stages/dedup.py)
to all nodes of each kind at once and folds duplicates directly in Neo4j.

The merged ids end up in the canonical node's `merged_ids` property, so a
later republish of an old group cannot resurrect a folded duplicate: the
loader re-folds any id found in a graph-side merged_ids map at the end of
every load (see jsonl_loader.load_jsonl_dir).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date
from pathlib import Path

from pauk.models import Person
from pauk.pipeline.normalize import _merge_person
from pauk.pipeline.stages.dedup import (
    PLACEHOLDER_TITLES,
    _grouped,
    _norm,
    _norm_doi,
    _union,
    plan_person_merges,
)
from pauk.settings import Settings
from pauk.storage.atomic import AtomicWriter
from pauk.urls import normalize_repo_url

from .client import Neo4jClient, chunked
from .extract import NODE_REGISTRY, extract_node

logger = logging.getLogger(__name__)

CANDIDATES_FILENAME = "dedup_candidates_graph.jsonl"


def _ordinal(value) -> int:
    """Comparable day number for a stored date value (string or date)."""
    if isinstance(value, date):
        return value.toordinal()
    try:
        return date.fromisoformat(str(value)).toordinal() if value else date.min.toordinal()
    except ValueError:
        return date.min.toordinal()


def _node_version(row: dict) -> dict:
    """A version-ledger entry for one Publication node as it stands now.

    Captured before a fold moves the node's AUTHORED edges away, so the
    entry records the author list this record itself carried.
    """
    authors = [
        {key: value for key, value in author.items() if value is not None}
        for author in sorted(
            row.get("authors") or [],
            key=lambda a: (a.get("position") is None, a.get("position"), a.get("person_id")),
        )
    ]
    entry = {
        "openalex_id": row["id"],
        "title": row.get("title"),
        "doi": row.get("doi"),
        "journal": row.get("journal"),
        "publication_date": str(row["publication_date"]) if row.get("publication_date") else None,
        "year": row.get("year"),
        "openalex_url": row.get("openalex_url"),
        "pdf_url": row.get("pdf_url"),
        "abstract": row.get("abstract"),
        "authors": authors or None,
    }
    return {key: value for key, value in entry.items() if value is not None}


def _merged_versions_json(canonical: dict, duplicates: list[dict]) -> str:
    """The canonical node's version ledger after folding `duplicates` in.

    Entries already in a ledger win (the per-group stage wrote them with the
    author list as of that merge); live node state only fills what is
    missing, and every folded record contributes its own entry.
    """
    by_id: dict[str, dict] = {}

    def absorb(entry: dict) -> None:
        existing = by_id.setdefault(entry.get("openalex_id"), entry)
        if existing is not entry:
            for key, value in entry.items():
                # An empty author list counts as missing: entries written
                # before author lists were versioned carry one.
                if existing.get(key) in (None, [], ""):
                    existing[key] = value

    for row in (canonical, *duplicates):
        try:
            stored = json.loads(row.get("versions") or "[]")
        except json.JSONDecodeError:
            stored = []
        for entry in stored:
            absorb(entry)
        absorb(_node_version(row))
    return json.dumps(list(by_id.values()), ensure_ascii=False)


def _keyed_pairs(rows: list[dict], key_functions) -> tuple[list[tuple[str, str]], dict[frozenset, str]]:
    """Pair node rows that share a key; remember which rule paired them."""
    pairs: list[tuple[str, str]] = []
    pair_rules: dict[frozenset, str] = {}
    for key_of, rule in key_functions:
        buckets: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            key = key_of(row)
            if key:
                buckets[key].append(row["id"])
        for bucket in buckets.values():
            unique = list(dict.fromkeys(bucket))
            for other in unique[1:]:
                pairs.append((unique[0], other))
                pair_rules.setdefault(frozenset((unique[0], other)), rule)
    return pairs, pair_rules


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


def dedup_graph_persons(client, raw_orcids: dict[str, str | None]) -> tuple[int, list[dict]]:
    """Fold duplicate Person nodes across all published groups.

    Args:
        client: An open Neo4jClient (or a compatible double).
        raw_orcids: Trusted ORCID per OpenAlex author id, from
            collect_raw_orcids().

    Returns:
        (removed, report): the number of folded nodes and the review
        journal rows (applied merges marked "merged", held-back pairs
        marked "held").
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
            department_ids=list(row.get("department_ids") or []),
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
    groups, report = plan_person_merges(
        people, trusted_orcid, fields_of=client.fetch_publication_fields())

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
    return removed, report


def dedup_graph_publications(client) -> tuple[int, list[dict]]:
    """Fold duplicate Publication nodes across all published groups.

    Same evidence as the per-group stage: records sharing a DOI or a
    non-placeholder title are one work; the best-documented, then latest
    record survives.
    """
    rows = client.fetch_publications_for_dedup()
    by_id = {row["id"]: row for row in rows}
    pairs, pair_rules = _keyed_pairs(rows, (
        (lambda row: _norm_doi(row.get("doi")), "doi"),
        (lambda row: (
            title if (title := _norm(row.get("title"))) not in PLACEHOLDER_TITLES else None
        ), "title"),
    ))

    merges: list[tuple[str, str]] = []
    canonical_updates: list[tuple[str, dict]] = []
    report: list[dict] = []
    for members in _grouped(pairs):
        ranked = sorted(
            (by_id[member] for member in members),
            # Same order as the per-group stage: the best-documented record
            # is the face, freshness only breaks ties.
            key=lambda row: (
                -(row.get("author_count") or 0),
                -_ordinal(row.get("publication_date")),
                row["id"],
            ),
        )
        canonical, duplicates = ranked[0], ranked[1:]
        merged_ids = list(canonical.get("merged_ids") or [])
        for duplicate in duplicates:
            logger.info("graph dedup: merging publication %s (%s) into %s (%s)",
                        duplicate["id"], duplicate.get("title"),
                        canonical["id"], canonical.get("title"))
            merged_ids = _union(merged_ids, duplicate.get("merged_ids") or [], [duplicate["id"]])
            merges.append((duplicate["id"], canonical["id"]))
            report.append({
                "status": "merged", "entity": "publication",
                "record_a": duplicate["id"], "name_a": duplicate.get("title"),
                "record_b": canonical["id"], "name_b": canonical.get("title"),
                "merged_into": canonical["id"],
                "rules": sorted({
                    rule for pair, rule in pair_rules.items() if duplicate["id"] in pair
                }),
            })
        canonical_updates.append((canonical["id"], {
            "merged_ids": merged_ids,
            # The folded records' venues, abstracts and author lists survive
            # on the canonical node; the graph itself is still drawn from the
            # merged, current state.
            "versions": _merged_versions_json(canonical, duplicates),
        }))

    for chunk in chunked(canonical_updates):
        client.upsert_nodes_batch("Publication", chunk)
    removed = 0
    for chunk in chunked(merges):
        removed += client.merge_publication_nodes_batch(chunk)
    return removed, report


def dedup_graph_repositories(client) -> tuple[int, list[dict]]:
    """Fold duplicate Repository nodes across all published groups.

    Same evidence as the per-group stage: GitHub's numeric id, one stored
    URL, or a URL cited by two rows (a rename between runs). The freshest
    node survives, then the most cited one.
    """
    rows = client.fetch_repositories_for_dedup()
    by_id = {row["id"]: row for row in rows}
    pairs, pair_rules = _keyed_pairs(rows, (
        (lambda row: str(row["github_id"]) if row.get("github_id") else None, "github_id"),
        (lambda row: normalize_repo_url(row["url"]) if row.get("url") else None, "url"),
    ))
    by_cited: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for cited in row.get("cited_urls") or []:
            by_cited[normalize_repo_url(cited)].append(row["id"])
    for bucket in by_cited.values():
        unique = list(dict.fromkeys(bucket))
        for other in unique[1:]:
            pairs.append((unique[0], other))
            pair_rules.setdefault(frozenset((unique[0], other)), "cited_url")

    merges: list[tuple[str, str]] = []
    canonical_updates: list[tuple[str, dict]] = []
    report: list[dict] = []
    for members in _grouped(pairs):
        ranked = sorted(
            (by_id[member] for member in members),
            key=lambda row: (
                -_ordinal(row.get("access_date")),
                -(row.get("publication_count") or 0),
                row["id"],
            ),
        )
        canonical, duplicates = ranked[0], ranked[1:]
        merged_ids = list(canonical.get("merged_ids") or [])
        cited_urls = list(canonical.get("cited_urls") or [])
        for duplicate in duplicates:
            logger.info("graph dedup: merging repository %s (%s) into %s (%s)",
                        duplicate["id"], duplicate.get("url"),
                        canonical["id"], canonical.get("url"))
            merged_ids = _union(merged_ids, duplicate.get("merged_ids") or [], [duplicate["id"]])
            cited_urls = _union(cited_urls, [duplicate.get("url")] if duplicate.get("url") else [],
                                duplicate.get("cited_urls") or [])
            merges.append((duplicate["id"], canonical["id"]))
            report.append({
                "status": "merged", "entity": "repository",
                "record_a": duplicate["id"], "name_a": duplicate.get("url"),
                "record_b": canonical["id"], "name_b": canonical.get("url"),
                "merged_into": canonical["id"],
                "rules": sorted({
                    rule for pair, rule in pair_rules.items() if duplicate["id"] in pair
                }),
            })
        canonical_updates.append((canonical["id"], {"merged_ids": merged_ids, "cited_urls": cited_urls}))

    for chunk in chunked(canonical_updates):
        client.upsert_nodes_batch("Repository", chunk)
    removed = 0
    for chunk in chunked(merges):
        removed += client.merge_repository_nodes_batch(chunk)
    return removed, report


def run_graph_dedup(config: Settings) -> dict[str, int]:
    """CLI entry point for `pauk dedup graph`: persons, publications and
    repositories deduplicated across every published group, with one
    combined review journal in the cache directory."""
    client = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
    try:
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        persons_removed, person_report = dedup_graph_persons(
            client, collect_raw_orcids(config.raw_dir))
        publications_removed, publication_report = dedup_graph_publications(client)
        repositories_removed, repository_report = dedup_graph_repositories(client)

        report = [
            {"entity": "person", **row} if "entity" not in row else row
            for row in (*person_report, *publication_report, *repository_report)
        ]
        held = sum(1 for row in report if row["status"] == "held")
        journal_path = config.cache_dir / CANDIDATES_FILENAME
        with AtomicWriter(journal_path) as fh:
            for row in report:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if report:
            logger.info("graph dedup: review journal in %s — %d merge(s) applied, %d pair(s) held",
                        journal_path,
                        persons_removed + publications_removed + repositories_removed, held)
        return {
            "graph_persons_merged": persons_removed,
            "graph_publications_merged": publications_removed,
            "graph_repositories_merged": repositories_removed,
            "graph_dedup_candidates": held,
        }
    finally:
        client.close()
