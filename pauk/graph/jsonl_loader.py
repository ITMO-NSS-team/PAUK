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

from .client import Neo4jClient, chunked
from .extract import NODE_REGISTRY, extract_node, extract_relationships

logger = logging.getLogger(__name__)


FILE_SPECS: dict[str, str] = {
    "departments.jsonl": "department",
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


def extract_repo_links(
    pub_links_row: dict, known_repository_urls: set[str]
) -> tuple[list[tuple[str, dict]], list[tuple[str, str, dict]]]:
    """Extract MENTIONS_LINK edges from one repo_links.jsonl row.

    A row here (PubLinks) is not a node — it's a flat list of candidate code
    links for one publication, and unlike Publication.mentions_links it
    carries no target_kind discriminator. The rule (matching the old
    graph_loader.py): if a link's url matches an already-known Repository.url,
    create a MENTIONS_LINK edge to that Repository (matched by url);
    otherwise create a LinkCandidate node on the fly, using the url itself
    as its id — repo_links.jsonl carries no other stable id for a candidate.

    Args:
        pub_links_row: One decoded repo_links.jsonl line
            ({"publication_id": ..., "links": [...]}).
        known_repository_urls: URLs of Repository nodes already seen while
            loading repositories.jsonl in this run.

    Returns:
        A (link_candidate_nodes, mentions_link_edges) tuple: nodes to add to
        the LinkCandidate batch, and (publication_id, target_id, props)
        edges to add to the relevant MENTIONS_LINK batch.
    """
    publication_id = pub_links_row["publication_id"]
    candidate_nodes: list[tuple[str, dict]] = []
    edges: list[tuple[str, str, dict]] = []

    for link in pub_links_row.get("links") or []:
        url = link.get("url")
        if not url:
            continue
        props = {
            k: link[k]
            for k in ("context", "page_number", "is_relevant", "llm_confidence", "llm_reason")
            if link.get(k) is not None
        }
        if url in known_repository_urls:
            edges.append((publication_id, url, props))  # target matched by "url"
        else:
            candidate_nodes.append((url, {"url": url, "host": link.get("host")}))
            edges.append((publication_id, url, props))  # target matched by "id" == url

    return candidate_nodes, edges


def load_jsonl_dir(client: Neo4jClient, in_dir: Path) -> None:
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
    rel_batches: dict[tuple[str, str, str, str], list[tuple[str, str, dict]]] = defaultdict(list)
    known_repository_urls: set[str] = set()

    for filename, spec_key in FILE_SPECS.items():
        path = in_dir / filename
        if not path.exists():
            logger.info("%s not found in %s, skipping", filename, in_dir)
            continue
        spec = NODE_REGISTRY[spec_key]
        for row in _read_jsonl(path):
            labels, node = extract_node(row, spec)
            node_batches[labels].append(node)
            if spec_key == "repository":
                # url is required on Repository, not Optional.
                known_repository_urls.add(row["url"])
            for key, rels in extract_relationships(row, spec).items():
                rel_batches[key].extend(rels)

    # Persons share a single file but use different labels in the graph.
    persons_path = in_dir / "persons.jsonl"
    if persons_path.exists():
        for row in _read_jsonl(persons_path):
            spec = NODE_REGISTRY["itmo_person" if row.get("is_itmo") else "external_person"]
            labels, node = extract_node(row, spec)
            node_batches[labels].append(node)
            for key, rels in extract_relationships(row, spec).items():
                rel_batches[key].extend(rels)

    repo_links_path = in_dir / "repo_links.jsonl"
    if repo_links_path.exists():
        mentions_key = ("Publication", "LinkCandidate", "MENTIONS_LINK", "id")
        mentions_repo_key = ("Publication", "Repository", "MENTIONS_LINK", "url")
        for row in _read_jsonl(repo_links_path):
            candidate_nodes, edges = extract_repo_links(row, known_repository_urls)
            node_batches["LinkCandidate"].extend(candidate_nodes)
            for src_id, tgt_id, props in edges:
                key = mentions_repo_key if tgt_id in known_repository_urls else mentions_key
                rel_batches[key].append((src_id, tgt_id, props))
    else:
        logger.info("repo_links.jsonl not found in %s, skipping", in_dir)

    for labels, nodes in node_batches.items():
        for chunk in chunked(nodes):
            client.upsert_nodes_batch(labels, chunk)
        logger.info("nodes (:%s): loaded %d", labels, len(nodes))

    for (src_label, tgt_label, rel_type, tgt_match_prop), rels in rel_batches.items():
        for chunk in chunked(rels):
            client.upsert_relationships_batch(src_label, tgt_label, rel_type, chunk, tgt_match_prop)
        logger.info("relationships (:%s)-[:%s]->(:%s): requested %d", src_label, rel_type, tgt_label, len(rels))
