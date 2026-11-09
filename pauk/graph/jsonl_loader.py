"""Load prepared entity rows into Neo4j.

Nodes must be loaded before relationships: Cypher MATCH skips missing endpoints.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

from pauk.aliases import flatten_merges
from pauk.urls import normalize_repo_url

from .audit import AuditedNeo4jClient
from .client import Neo4jClient, chunked
from .extract import NODE_REGISTRY, extract_node, extract_relationships, person_spec

__all__ = ["extract_repo_links", "load_prepared_rows", "normalize_repo_url"]

logger = logging.getLogger(__name__)

# The worker's callback also checks for cancellation between batches.
Progress = Callable[[str, int, int], None]


def _tick(report: Progress | None, step: str, done: int, total: int) -> None:
    """Report progress and check cancellation; committed batches remain for a retry."""
    if report is not None:
        report(step, done, total)


FILE_SPECS: dict[str, str] = {
    "departments.jsonl": "department",
    "organizations.jsonl": "organization",
    "publications.jsonl": "publication",
    "repositories.jsonl": "repository",
    "github_profiles.jsonl": "github_profile",
}


def _stage_failed(row: dict, stage: str) -> bool:
    """True if the given enrichment stage is recorded as failed on this row."""
    state = (row.get("_processing") or {}).get(stage) or {}
    return state.get("status") == "failed"


def _stage_succeeded(row: dict, stage: str) -> bool:
    state = (row.get("_processing") or {}).get(stage) or {}
    return state.get("status") in {"completed", "completed_empty"}


def _sync_github_withdrawals(client, rows, person_aliases, repository_aliases,
                             dropped_relationships, claimed_relationships) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[person_aliases.get(row["id"], row["id"])].append(row)

    def contribution_pair(person_id, repository_id):
        return person_aliases.get(person_id, person_id), repository_aliases.get(repository_id, repository_id)

    def edge_pairs(edges):
        return {contribution_pair(src_id, tgt_id) for src_label, rel_type, tgt_label, src_id, tgt_id in edges or ()
                if (src_label, rel_type, tgt_label) == ("Person", "CONTRIBUTED_TO", "Repository")}

    protected = edge_pairs(claimed_relationships)
    dropped = edge_pairs(dropped_relationships)
    patches = []
    removed_pairs = set()
    desired = {}
    for canonical, sources in grouped.items():
        if not any(row.get("github_match") for row in sources):
            continue
        sources.sort(key=lambda row: (row["id"] != canonical, row["id"]))
        folded = any(row["id"] != canonical for row in sources)
        cleared = {contribution_pair(row["id"], item["repository_id"])
                   for row in sources for item in (row.get("github_match") or {}).get("cleared_contributions") or []}
        if folded:
            # A null on a resurrected alias does not erase the survivor during the fold.
            props = {}
            for field in ("github", "email"):
                values = [(row.get(field) == (row.get("github_match") or {}).get(field), row[field])
                          for row in sources if row.get(field) is not None]
                if values:
                    props[field] = min(values, key=lambda value: value[0])[1]
                elif any(field in (row.get("github_match") or {}).get("cleared_fields", ()) for row in sources):
                    props[field] = None
            if props:
                patches.append((canonical, props))
        current = {}
        for row in sources:
            generated = (row.get("github_match") or {}).get("contributed_to") or []
            for item in row.get("contributed_to") or []:
                pair = contribution_pair(row["id"], item["repository_id"])
                rank = item in generated, item.get("role") is None
                if pair not in current or rank < current[pair][0]:
                    current[pair] = rank, {"role": item["role"]} if item.get("role") is not None else {}
        removed_pairs.update(cleared - current.keys() - protected)
        if cleared or folded:
            desired.update({pair: props for pair, (_rank, props) in current.items() if pair not in dropped})
    for chunk in chunked(patches):
        client.upsert_nodes_batch("Person", chunk)
    for chunk in chunked([(source, target, props) for (source, target), props in desired.items()]):
        client.upsert_relationships_batch("Person", "Repository", "CONTRIBUTED_TO", chunk)
    for chunk in chunked(sorted(removed_pairs)):
        client.delete_relationships_batch("Person", "Repository", "CONTRIBUTED_TO", chunk)


def extract_repo_links(
    pub_links_row: dict,
    known_repository_urls: dict[str, str],
    *,
    dropped_candidates: frozenset[str] | set[str] = frozenset(),
    synchronize_relevance: bool | None = None,
) -> tuple[
    list[tuple[str, dict]],
    list[tuple[str, str, dict]],
    list[tuple[str, str, dict]],
    list[tuple[str, str]],
]:
    """Extract MENTIONS_LINK edges from one repo_links.jsonl row.

    Known repository URLs resolve to Repository nodes; other URLs become
    LinkCandidate ids. Candidate promotions redirect edges from earlier loads.

    Args:
        known_repository_urls: Normalized URL to the URL stored on the node.
        dropped_candidates: Manually deleted candidates, which have no prepared
            rows for the loader's tombstone filter to exclude.
        synchronize_relevance: True replaces verdicts, including nulls;
            False preserves them after a failed classification; None applies
            the legacy non-null-only update.

    Returns:
        Candidate nodes, repository edges matched by URL, candidate edges
        matched by id, and (candidate_id, repository_url) promotions.
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
        relevance_fields = (
            "classification_status",
            "is_relevant",
            "llm_confidence",
            "llm_reason",
        )
        if synchronize_relevance is True:
            # Nulls clear the old verdict when reclassification becomes uncertain.
            props = {key: link.get(key) for key in relevance_fields}
        elif synchronize_relevance is False:
            props = {}
        else:
            props = {key: link[key] for key in relevance_fields if link.get(key) is not None}
            if props.get("classification_status") == "pending":
                props.pop("classification_status")
        occurrences = link.get("occurrences") or []
        ambiguous = bool(occurrences) and all(
            len(occurrence.get("candidate_urls") or []) > 1 for occurrence in occurrences
        )
        if any(occurrence.get("raw_url") for occurrence in occurrences):
            props["url_ambiguous"] = ambiguous
            props["candidate_urls"] = sorted({
                candidate for occurrence in occurrences
                for candidate in occurrence.get("candidate_urls") or []
            })
            props["availability"] = link.get("availability", "unchecked")
        if ambiguous:
            props["is_relevant"] = None
        if occurrences:
            props["context"] = [o.get("context") or "" for o in occurrences]
            props["page_number"] = [o.get("page_number") or 0 for o in occurrences]
            props["pdf_id"] = [o.get("pdf_id") or "" for o in occurrences]
            if any(o.get("source") for o in occurrences):
                props["context_source"] = [o.get("source") or "" for o in occurrences]
            if any(o.get("reference_label") for o in occurrences):
                props["reference_label"] = [o.get("reference_label") or "" for o in occurrences]
        stored_url = known_repository_urls.get(normalize_repo_url(url))
        if stored_url is not None:
            repo_edges.append((publication_id, stored_url, props))
            candidate_promotions.append((url, stored_url))
        elif url not in dropped_candidates:
            candidate_nodes.append((url, {"url": url, "host": link.get("host")}))
            candidate_edges.append((publication_id, url, props))

    return candidate_nodes, repo_edges, candidate_edges, candidate_promotions


def _keep_graph_merges(client, label: str, nodes: list[tuple[str, dict]],
                       aliases: dict[str, str] | None = None) -> int:
    """Preserve graph-only merged_ids so republishing cannot undo a graph fold.

    Returns:
        How many nodes kept an id their row does not carry.
    """
    if not nodes:
        return 0
    held: dict[str, list[str]] = defaultdict(list)
    if aliases is None:
        aliases = client.fetch_merged_id_map(label)
    for alias, canonical in aliases.items():
        if alias != canonical:
            held[canonical].append(alias)
    kept = 0
    for node_id, props in nodes:
        known = props.get("merged_ids") or []
        extra = [alias for alias in held.get(node_id, ()) if alias not in known]
        if extra:
            props["merged_ids"] = [*known, *extra]
            kept += 1
    if kept:
        logger.info("nodes (:%s): %d node(s) kept a fold their row does not carry", label, kept)
    return kept


def load_prepared_rows(
    client: Neo4jClient | AuditedNeo4jClient,
    rows_by_file: dict[str, list[dict]],
    dropped_relationships: set[tuple[str, str, str, str, str]] | None = None,
    dropped_candidates: set[str] | None = None,
    report: Progress | None = None,
    claimed_relationships: set[tuple[str, str, str, str, str]] | None = None,
) -> None:
    """Load nodes, relationships, then folds and explicit withdrawals in batches.

    Args:
        rows_by_file: Prepared rows keyed by entity filename; missing files are empty.
        dropped_relationships: Manually unlinked edges, as
            (src_label, rel_type, tgt_label, src_id, tgt_id), skipped before upserts.
        dropped_candidates: Manually deleted LinkCandidate ids.
        report: Progress callback; may raise to cancel between batches.
        claimed_relationships: Edges added by hand, protected from automatic cleanup.
    """
    node_batches: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    person_nodes: list[tuple[str, dict]] = []
    rel_batches: dict[tuple[str, str, str, str], list[tuple[str, str, dict]]] = defaultdict(list)
    known_repository_urls: dict[str, str] = {}
    candidate_promotions: dict[str, str] = {}
    repository_ids_by_url: dict[str, str] = {}
    mentions_scope = {
        row["id"] for row in rows_by_file.get("publications.jsonl") or []
        if _stage_succeeded(row, "code_links")
    } & {
        row["publication_id"] for row in rows_by_file.get("repo_links.jsonl") or []
        if isinstance(row.get("links"), list)
    }
    node_merges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    implements_scope = {
        row["id"]
        for row in rows_by_file.get("publications.jsonl") or []
        if _stage_succeeded(row, "link_relevance")
    }
    failed_relevance_scope = {
        row["id"]
        for row in rows_by_file.get("publications.jsonl") or []
        if _stage_failed(row, "link_relevance")
    }
    desired_implements: dict[str, set[str]] = {
        publication_id: set() for publication_id in implements_scope
    }

    for filename, spec_key in FILE_SPECS.items():
        rows = rows_by_file.get(filename)
        if not rows:
            logger.info("%s: no rows, skipping", filename)
            continue
        spec = NODE_REGISTRY[spec_key]
        skipped_failed = 0
        for row in rows:
            if spec_key == "repository":
                repository_ids_by_url[row["url"]] = row["id"]
                for publication_id in row.get("publication_ids") or []:
                    if publication_id in implements_scope:
                        desired_implements[publication_id].add(row["id"])
            if spec_key == "repository" and _stage_failed(row, "repositories"):
                # Never enriched successfully: a name/url stub would pollute
                # the graph; it gets loaded once a retry succeeds.
                skipped_failed += 1
                continue
            labels, node = extract_node(row, spec)
            if spec_key == "publication":
                if row["id"] in implements_scope:
                    # Successful reclassification must clear an obsolete code_url.
                    node[1]["code_url"] = row.get("code_url")
                elif row["id"] in failed_relevance_scope:
                    # Keep the graph's last complete publication-level verdict.
                    node[1].pop("has_code", None)
                    node[1].pop("code_url", None)
            node_batches[labels].append(node)
            for merged_id in row.get("merged_ids") or []:
                node_merges[spec_key].append((merged_id, row["id"]))
            if spec_key == "repository":
                # Resolve URLs cited before repository renames or canonicalization.
                known_repository_urls[normalize_repo_url(row["url"])] = row["url"]
                for cited in row.get("cited_urls") or []:
                    known_repository_urls.setdefault(normalize_repo_url(cited), row["url"])
            for key, rels in extract_relationships(row, spec).items():
                if key[2] == "MENTIONS_LINK" and row["id"] in mentions_scope:
                    # A completed extraction supersedes the legacy publication field.
                    continue
                rel_batches[key].extend(rels)
        if skipped_failed:
            logger.info("%s: skipped %d failed (never enriched) row(s)", filename, skipped_failed)

    # Both person specs share one file and label but allow different relationships.
    person_merges: list[tuple[str, str]] = []
    for row in rows_by_file.get("persons.jsonl") or ():
        spec = person_spec(row)
        _labels, node = extract_node(row, spec)
        # Explicit withdrawals must reach SET += as nulls; an unknown source value must not erase the graph.
        for field in (row.get("github_match") or {}).get("cleared_fields") or ():
            if field in ("github", "email") and row.get(field) is None:
                node[1][field] = None
        person_nodes.append(node)
        for merged_id in row.get("merged_ids") or []:
            person_merges.append((merged_id, row["id"]))
        for key, rels in extract_relationships(row, spec).items():
            rel_batches[key].extend(rels)

    repo_links_rows = rows_by_file.get("repo_links.jsonl")
    if repo_links_rows:
        mentions_key = ("Publication", "LinkCandidate", "MENTIONS_LINK", "id")
        mentions_repo_key = ("Publication", "Repository", "MENTIONS_LINK", "url")
        for row in repo_links_rows:
            publication_id = row["publication_id"]
            relevance_sync = (
                True
                if publication_id in implements_scope
                else (False if publication_id in failed_relevance_scope else None)
            )
            candidate_nodes, repo_edges, candidate_edges, promotions = extract_repo_links(
                row,
                known_repository_urls,
                dropped_candidates=dropped_candidates or frozenset(),
                synchronize_relevance=relevance_sync,
            )
            node_batches["LinkCandidate"].extend(candidate_nodes)
            rel_batches[mentions_repo_key].extend(repo_edges)
            rel_batches[mentions_key].extend(candidate_edges)
            candidate_promotions.update(promotions)
    else:
        logger.info("repo_links.jsonl: no rows, skipping")

    known_merges = {
        label: client.fetch_merged_id_map(label) for label in ("Person", "Publication", "Repository")
    }
    # Validate the complete alias graph before writes and before splitting it
    # into chunks: a chain can span two different chunks or source groups.
    merge_plans = {
        label: flatten_merges([*pairs, *known_merges[label].items()])
        for label, pairs in (
            ("Person", person_merges),
            ("Publication", node_merges["publication"]),
            ("Repository", node_merges["repository"]),
        )
    }
    _keep_graph_merges(client, "Person", person_nodes, known_merges["Person"])
    for label in ("Publication", "Repository"):
        _keep_graph_merges(client, label, node_batches.get(label) or [], known_merges[label])

    node_total = len(person_nodes) + sum(len(nodes) for nodes in node_batches.values())
    written = 0
    for labels, nodes in node_batches.items():
        for chunk in chunked(nodes):
            client.upsert_nodes_batch(labels, chunk)
            written += len(chunk)
            _tick(report, "выкладка узлов", written, node_total)
        logger.info("nodes (:%s): loaded %d", labels, len(nodes))

    for chunk in chunked(person_nodes):
        client.upsert_person_nodes_batch(chunk)
        written += len(chunk)
        _tick(report, "выкладка узлов", written, node_total)
    itmo_count = sum(1 for _, props in person_nodes if props.get("is_itmo"))
    logger.info(
        "nodes (:Person): loaded %d (itmo=%d, external=%d)",
        len(person_nodes), itmo_count, len(person_nodes) - itmo_count,
    )

    # Redirect earlier candidate edges now that their repositories are known.
    for chunk in chunked(list(candidate_promotions.items())):
        client.promote_link_candidates_batch(chunk)

    rel_total = sum(len(rels) for rels in rel_batches.values())
    linked = 0
    for (src_label, tgt_label, rel_type, tgt_match_prop), rels in rel_batches.items():
        if dropped_relationships:
            kept = [(src_id, tgt_id, props) for src_id, tgt_id, props in rels
                    if (src_label, rel_type, tgt_label, src_id, tgt_id) not in dropped_relationships]
            if len(kept) != len(rels):
                logger.info("relationships (:%s)-[:%s]->(:%s): %d skipped as unlinked by hand",
                            src_label, rel_type, tgt_label, len(rels) - len(kept))
                # Skipped edges still count toward completed progress.
                linked += len(rels) - len(kept)
            rels = kept
        for chunk in chunked(rels):
            client.upsert_relationships_batch(src_label, tgt_label, rel_type, chunk, tgt_match_prop)
            linked += len(chunk)
            _tick(report, "выкладка связей", linked, rel_total)
        logger.info("relationships (:%s)-[:%s]->(:%s): requested %d", src_label, rel_type, tgt_label, len(rels))

    # Fold only after upserts: an incoming relationship may still name an
    # alias present in this batch. Deleting it earlier makes MATCH lose that edge.
    for chunk in chunked(person_merges):
        client.merge_person_nodes_batch(chunk)
    for chunk in chunked(node_merges["publication"]):
        client.merge_publication_nodes_batch(chunk)
    for chunk in chunked(node_merges["repository"]):
        client.merge_repository_nodes_batch(chunk)

    # Reapply graph-only folds after an older group's rows recreated their aliases.
    merged_id_maps: dict[str, dict[str, str]] = {}
    for label, fold in (
        ("Person", client.merge_person_nodes_batch),
        ("Publication", client.merge_publication_nodes_batch),
        ("Repository", client.merge_repository_nodes_batch),
    ):
        merged_id_map = dict(merge_plans[label])
        merged_id_maps[label] = merged_id_map
        for chunk in chunked(merge_plans[label]):
            fold(chunk)

    publication_aliases = merged_id_maps["Publication"]
    repository_aliases = merged_id_maps["Repository"]
    _sync_github_withdrawals(client, rows_by_file.get("persons.jsonl") or [],
                             merged_id_maps["Person"], repository_aliases,
                             dropped_relationships, claimed_relationships)
    desired_mentions: dict[str, tuple[set[str], set[str]]] = {}
    for publication_id in mentions_scope:
        canonical_id = publication_aliases.get(publication_id, publication_id)
        desired_mentions.setdefault(canonical_id, (set(), set()))
    # A folded publication with an incomplete result must not lose its evidence.
    for row in rows_by_file.get("publications.jsonl") or []:
        if row["id"] not in mentions_scope:
            desired_mentions.pop(publication_aliases.get(row["id"], row["id"]), None)

    def keep_mention(publication_id: str, target_label: str, target_id: str) -> None:
        canonical_id = publication_aliases.get(publication_id, publication_id)
        if canonical_id not in desired_mentions:
            return
        repositories, candidates = desired_mentions[canonical_id]
        if target_label == "LinkCandidate" and target_id in candidate_promotions:
            target_label, target_id = "Repository", candidate_promotions[target_id]
        if target_label == "Repository":
            repository_id = repository_ids_by_url.get(target_id, target_id)
            repositories.add(repository_aliases.get(repository_id, repository_id))
        elif target_label == "LinkCandidate":
            candidates.add(target_id)

    for (src_label, tgt_label, rel_type, _), rels in rel_batches.items():
        if src_label == "Publication" and rel_type == "MENTIONS_LINK":
            for src_id, tgt_id, _ in rels:
                keep_mention(src_id, tgt_label, tgt_id)
    for src_label, rel_type, tgt_label, src_id, tgt_id in claimed_relationships or ():
        if src_label == "Publication" and rel_type == "MENTIONS_LINK":
            keep_mention(src_id, tgt_label, tgt_id)
    mentions_rows = [
        (publication_id, sorted(repositories), sorted(candidates))
        for publication_id, (repositories, candidates) in sorted(desired_mentions.items())
    ]
    removed_mentions = 0
    synchronized_mentions = 0
    for chunk in chunked(mentions_rows):
        removed_mentions += client.sync_mentions_links_batch(chunk)
        synchronized_mentions += len(chunk)
        _tick(report, "синхронизация упоминаний", synchronized_mentions, len(mentions_rows))
    if mentions_rows:
        logger.info("MENTIONS_LINK: synchronized %d publication(s), removed %d stale edge(s)",
                    len(mentions_rows), removed_mentions)

    resolved_implements: dict[str, set[str]] = defaultdict(set)
    for publication_id, repository_ids in desired_implements.items():
        canonical_publication_id = publication_aliases.get(publication_id, publication_id)
        resolved_implements[canonical_publication_id].update(
            repository_aliases.get(repository_id, repository_id)
            for repository_id in repository_ids
        )
    # Manual claims are independent provenance, not stale model verdicts.
    for src_label, rel_type, tgt_label, src_id, tgt_id in claimed_relationships or ():
        if (src_label, rel_type, tgt_label) != ("Repository", "IMPLEMENTS", "Publication"):
            continue
        canonical_publication_id = publication_aliases.get(tgt_id, tgt_id)
        if canonical_publication_id in resolved_implements:
            resolved_implements[canonical_publication_id].add(repository_aliases.get(src_id, src_id))
    implements_rows = [
        (publication_id, sorted(repository_ids))
        for publication_id, repository_ids in sorted(resolved_implements.items())
    ]
    removed_implements = 0
    for chunk in chunked(implements_rows):
        removed_implements += client.sync_implements_relationships_batch(chunk)
    if implements_rows:
        logger.info(
            "relationships (:Repository)-[:IMPLEMENTS]->(:Publication): "
            "synchronized %d publication(s), removed %d stale edge(s)",
            len(implements_rows),
            removed_implements,
        )
