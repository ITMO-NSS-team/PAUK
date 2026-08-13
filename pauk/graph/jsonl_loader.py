"""Load a prepared-JSONL group (data/prepared/<group>/) into Neo4j.

Loading is strictly nodes-first, relationships-second: if a relationship
targets a node that hasn't been loaded, Cypher's MATCH simply won't find it
and the relationship doesn't get created (see client.py, which logs a
warning with the exact count instead of silently dropping it — missing
target nodes are never auto-created as stubs).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from pauk.urls import normalize_repo_url

from .audit import AuditedNeo4jClient
from .client import Neo4jClient, chunked
from .extract import NODE_REGISTRY, extract_node, extract_relationships

__all__ = ["extract_repo_links", "load_jsonl_dir", "normalize_repo_url"]

logger = logging.getLogger(__name__)


FILE_SPECS: dict[str, str] = {
    "departments.jsonl": "department",
    "organizations.jsonl": "organization",
    "publications.jsonl": "publication",
    "repositories.jsonl": "repository",
    "github_profiles.jsonl": "github_profile",
}


def _read_jsonl(path: Path) -> Iterator[dict]:
    """Yield each non-empty line of a JSONL file as a decoded dict."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def _stage_failed(row: dict, stage: str) -> bool:
    """True if the given enrichment stage is recorded as failed on this row."""
    state = (row.get("_processing") or {}).get(stage) or {}
    return state.get("status") == "failed"


def extract_repo_links(
    pub_links_row: dict, known_repository_urls: dict[str, str]
) -> tuple[
    list[tuple[str, dict]],
    list[tuple[str, str, dict]],
    list[tuple[str, str, dict]],
    list[tuple[str, str]],
]:
    """Extract MENTIONS_LINK edges from one repo_links.jsonl row.

    A row here (PubLinks) is not a node — it's a flat list of candidate code
    links for one publication, and unlike Publication.mentions_links it
    carries no target_kind discriminator. The rule (matching the old
    graph_loader.py): if a link's url matches an already-known Repository.url
    (compared via normalize_repo_url), create a MENTIONS_LINK edge to that
    Repository, matched by the repository's *stored* url; otherwise create a
    LinkCandidate node on the fly, using the url itself as its id —
    repo_links.jsonl carries no other stable id for a candidate.

    Args:
        pub_links_row: One decoded repo_links.jsonl line
            ({"publication_id": ..., "links": [...]}).
        known_repository_urls: Mapping of normalized Repository URL to the
            URL as stored on the Repository node, built while loading
            repositories.jsonl in this run.

    Returns:
        A (link_candidate_nodes, repository_edges, candidate_edges,
        candidate_promotions) tuple:
        LinkCandidate nodes to create, (publication_id, repository_url,
        props) edges matched by Repository "url", and (publication_id,
        candidate_id, props) edges matched by LinkCandidate "id".
        candidate_promotions maps a previously created candidate ID to its
        now-known Repository URL so the loader can migrate old graph edges.
    """
    publication_id = pub_links_row["publication_id"]
    candidate_nodes: list[tuple[str, dict]] = []
    repo_edges: list[tuple[str, str, dict]] = []
    candidate_edges: list[tuple[str, str, dict]] = []
    candidate_promotions: list[tuple[str, str]] = []

    for link in pub_links_row.get("links") or []:
        url = link.get("url")
        if not url:
            continue
        props = {
            k: link[k]
            for k in ("is_relevant", "llm_confidence", "llm_reason")
            if link.get(k) is not None
        }
        occurrences = link.get("occurrences") or []
        if occurrences:
            props["context"] = [o.get("context") or "" for o in occurrences]
            props["page_number"] = [o.get("page_number") or 0 for o in occurrences]
        stored_url = known_repository_urls.get(normalize_repo_url(url))
        if stored_url is not None:
            repo_edges.append((publication_id, stored_url, props))
            candidate_promotions.append((url, stored_url))
        else:
            candidate_nodes.append((url, {"url": url, "host": link.get("host")}))
            candidate_edges.append((publication_id, url, props))

    return candidate_nodes, repo_edges, candidate_edges, candidate_promotions


def load_jsonl_dir(client: Neo4jClient | AuditedNeo4jClient, in_dir: Path) -> None:
    """Load every prepared JSONL file found in `in_dir` into Neo4j.

    Reads all files first, accumulating nodes and relationships in memory
    (the dataset is thousands of rows, not millions, so this is simpler than
    interleaving reads with uploads), then uploads all nodes, then all
    relationships — both in chunks of client.CHUNK_SIZE.

    Args:
        client: An open Neo4jClient to load data into.
        in_dir: A prepared-JSONL group directory, e.g.
            data/prepared/<group>/.
    """
    node_batches: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    person_batches: dict[bool, list[tuple[str, dict]]] = {True: [], False: []}
    rel_batches: dict[tuple[str, str, str, str], list[tuple[str, str, dict]]] = defaultdict(list)
    known_repository_urls: dict[str, str] = {}
    candidate_promotions: dict[str, str] = {}
    node_merges: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for filename, spec_key in FILE_SPECS.items():
        path = in_dir / filename
        if not path.exists():
            logger.info("%s not found in %s, skipping", filename, in_dir)
            continue
        spec = NODE_REGISTRY[spec_key]
        skipped_failed = 0
        for row in _read_jsonl(path):
            if spec_key == "repository" and _stage_failed(row, "repositories"):
                # Never enriched successfully — a name/url stub would pollute
                # the graph; it gets loaded once a retry succeeds.
                skipped_failed += 1
                continue
            labels, node = extract_node(row, spec)
            node_batches[labels].append(node)
            for merged_id in row.get("merged_ids") or []:
                node_merges[spec_key].append((merged_id, row["id"]))
            if spec_key == "repository":
                # url is required on Repository, not Optional. cited_urls are
                # the URLs the repo was referenced by before canonicalization
                # (renames, case variants) — map them to the stored url too.
                known_repository_urls[normalize_repo_url(row["url"])] = row["url"]
                for cited in row.get("cited_urls") or []:
                    known_repository_urls.setdefault(normalize_repo_url(cited), row["url"])
            for key, rels in extract_relationships(row, spec).items():
                rel_batches[key].extend(rels)
        if skipped_failed:
            logger.info("%s: skipped %d failed (never enriched) row(s)", filename, skipped_failed)

    # Persons share a single file but use different labels in the graph.
    # They are merged on the base :Person label (see upsert_person_nodes_batch)
    # because the same author may be ITMO in one group and external in another.
    person_merges: list[tuple[str, str]] = []
    persons_path = in_dir / "persons.jsonl"
    if persons_path.exists():
        for row in _read_jsonl(persons_path):
            is_itmo = bool(row.get("is_itmo"))
            spec = NODE_REGISTRY["itmo_person" if is_itmo else "external_person"]
            _labels, node = extract_node(row, spec)
            person_batches[is_itmo].append(node)
            for merged_id in row.get("merged_ids") or []:
                person_merges.append((merged_id, row["id"]))
            for key, rels in extract_relationships(row, spec).items():
                rel_batches[key].extend(rels)

    repo_links_path = in_dir / "repo_links.jsonl"
    if repo_links_path.exists():
        mentions_key = ("Publication", "LinkCandidate", "MENTIONS_LINK", "id")
        mentions_repo_key = ("Publication", "Repository", "MENTIONS_LINK", "url")
        for row in _read_jsonl(repo_links_path):
            candidate_nodes, repo_edges, candidate_edges, promotions = extract_repo_links(row, known_repository_urls)
            node_batches["LinkCandidate"].extend(candidate_nodes)
            rel_batches[mentions_repo_key].extend(repo_edges)
            rel_batches[mentions_key].extend(candidate_edges)
            candidate_promotions.update(promotions)
    else:
        logger.info("repo_links.jsonl not found in %s, skipping", in_dir)

    for labels, nodes in node_batches.items():
        for chunk in chunked(nodes):
            client.upsert_nodes_batch(labels, chunk)
        logger.info("nodes (:%s): loaded %d", labels, len(nodes))

    for is_itmo, nodes in person_batches.items():
        for chunk in chunked(nodes):
            client.upsert_person_nodes_batch(chunk, is_itmo)
        logger.info("nodes (:Person:%s): loaded %d", "Itmo" if is_itmo else "External", len(nodes))

    # A previous publish may have created LinkCandidates while GitHub was
    # unavailable. Once the repository is known, move those old edges to the
    # Repository and delete candidates that became orphaned.
    for chunk in chunked(list(candidate_promotions.items())):
        client.promote_link_candidates_batch(chunk)

    # A previous publish may still hold nodes that the dedup stage has since
    # folded into a canonical row — migrate their relationships and remove
    # them before the canonical relationships are loaded.
    for chunk in chunked(person_merges):
        client.merge_person_nodes_batch(chunk)
    for chunk in chunked(node_merges["publication"]):
        client.merge_publication_nodes_batch(chunk)
    for chunk in chunked(node_merges["repository"]):
        client.merge_repository_nodes_batch(chunk)

    for (src_label, tgt_label, rel_type, tgt_match_prop), rels in rel_batches.items():
        for chunk in chunked(rels):
            client.upsert_relationships_batch(src_label, tgt_label, rel_type, chunk, tgt_match_prop)
        logger.info("relationships (:%s)-[:%s]->(:%s): requested %d", src_label, rel_type, tgt_label, len(rels))

    # A group published before a graph-wide dedup still carries rows for
    # ids that were since folded into another group's canonical node — the
    # upserts above just resurrected them, relationships included. Fold
    # them right back using the merged_ids maps stored on canonical nodes.
    for label, fold in (
        ("Person", client.merge_person_nodes_batch),
        ("Publication", client.merge_publication_nodes_batch),
        ("Repository", client.merge_repository_nodes_batch),
    ):
        alias_pairs = [
            (merged_id, canonical_id)
            for merged_id, canonical_id in client.fetch_merged_id_map(label).items()
            if merged_id != canonical_id
        ]
        for chunk in chunked(alias_pairs):
            fold(chunk)
