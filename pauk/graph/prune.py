"""Compare the graph with prepared data and remove stale nodes and edges.

Replay the loader through a recording client to reuse its publication rules.
Planning forwards reads only; writes update the expected state in memory.
Manual claims, pending folds and nodes with existing source rows are preserved,
even when the loader skips a row after failed enrichment.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from pymongo.database import Database

from pauk.jobs.locks import held
from pauk.jobs.models import GITHUB_MATCH, GRAPH
from pauk.storage.prepared import PreparedStore

from .client import chunked
from .jsonl_loader import load_prepared_rows
from .load import ENTITY_FILES, FILE_LABELS, _drop_tombstoned
from .mutations import RELATIONSHIPS
from .overrides import DELETE, LINK, tombstoned_ids, tombstoned_relationships

logger = logging.getLogger(__name__)

#: A relationship as both sides of the comparison name it.
Triple = tuple[str, str, str]


@dataclass
class Plan:
    """What a prune would remove, before anything is removed."""

    nodes: dict[str, list[str]] = field(default_factory=dict)
    edges: dict[Triple, list[tuple[str, str]]] = field(default_factory=dict)
    #: Records the rows do not explain but somebody claimed by hand.
    kept_by_hand: int = 0
    #: Ids the next publish will fold into another record rather than drop.
    folding: int = 0
    #: What was actually removed. Empty until a run applies the plan.
    removed: dict[str, int] = field(default_factory=dict)

    def total(self) -> int:
        return (sum(len(ids) for ids in self.nodes.values())
                + sum(len(pairs) for pairs in self.edges.values()))

    def counts(self) -> dict[str, int]:
        """The numbers a run hands back, for the page and the log."""
        return {
            "prune_nodes": sum(len(ids) for ids in self.nodes.values()),
            "prune_relationships": sum(len(pairs) for pairs in self.edges.values()),
            "prune_kept_by_hand": self.kept_by_hand,
        } | self.removed


class _WouldWrite:
    """Record the loader's expected nodes and edges without writing to Neo4j.

    Only fetch_* methods delegate to the real client.
    """

    def __init__(self, client, rows_by_file: dict[str, list[dict]] | None = None) -> None:
        self._client = client
        self.nodes: dict[str, set[str]] = defaultdict(set)
        self.edges: dict[Triple, set[tuple[str, str]]] = defaultdict(set)
        #: Ids a publish would fold: not stale, and not there afterwards.
        self.folding: set[str] = set()
        self.aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.repository_ids = {row["url"]: row["id"]
                               for row in (rows_by_file or {}).get("repositories.jsonl") or []}

    def __getattr__(self, name: str):
        # New loader writes must be handled explicitly, never forwarded during a preview.
        if not name.startswith("fetch_"):
            raise AttributeError(f"a prune preview cannot forward {name}")
        return getattr(self._client, name)

    def upsert_nodes_batch(self, labels, nodes: list[tuple[str, dict]]) -> None:
        label = labels if isinstance(labels, str) else ":".join(labels)
        self.nodes[label.split(":")[0]].update(node_id for node_id, _ in nodes)

    def upsert_person_nodes_batch(self, nodes: list[tuple[str, dict]]) -> None:
        self.nodes["Person"].update(node_id for node_id, _ in nodes)

    def upsert_relationships_batch(self, src_label: str, tgt_label: str, rel_type: str,
                                   relationships, tgt_match_prop: str = "id") -> int:
        self.edges[(src_label, rel_type, tgt_label)].update(
            (src_id, tgt_id) for src_id, tgt_id, _props in relationships)
        return len(relationships)

    def _fold(self, label: str, merges: list[tuple[str, str]]) -> int:
        self.folding.update(duplicate for duplicate, _canonical in merges)
        self.aliases[label].update(merges)
        return 0

    def merge_person_nodes_batch(self, merges) -> int:
        return self._fold("Person", merges)

    def merge_publication_nodes_batch(self, merges) -> int:
        return self._fold("Publication", merges)

    def merge_repository_nodes_batch(self, merges) -> int:
        return self._fold("Repository", merges)

    def _retain_edges(self, triple: Triple, keep: Callable[[tuple[str, str]], bool]) -> int:
        before = self.edges[triple]
        self.edges[triple] = {pair for pair in before if keep(pair)}
        return len(before) - len(self.edges[triple])

    def delete_relationships_batch(self, src_label, tgt_label, rel_type, pairs, tgt_match_prop="id") -> int:
        removed = set(pairs)
        sources = self.aliases[src_label]
        targets = self.aliases[tgt_label] if tgt_match_prop == "id" else {}
        return self._retain_edges((src_label, rel_type, tgt_label),
                                  lambda pair: (sources.get(pair[0], pair[0]), targets.get(pair[1], pair[1])) not in removed)

    def sync_implements_relationships_batch(self, publications) -> int:
        wanted = {publication: set(repositories) for publication, repositories in publications}

        def keep(pair):
            repository = self.aliases["Repository"].get(pair[0], pair[0])
            publication = self.aliases["Publication"].get(pair[1], pair[1])
            return publication not in wanted or repository in wanted[publication]

        return self._retain_edges(("Repository", "IMPLEMENTS", "Publication"), keep)

    def sync_mentions_links_batch(self, publications) -> int:
        wanted = {publication: (set(repositories), set(candidates))
                  for publication, repositories, candidates in publications}
        removed = 0
        for label in ("Repository", "LinkCandidate"):
            def keep(pair, label=label):
                publication = self.aliases["Publication"].get(pair[0], pair[0])
                if publication not in wanted:
                    return True
                repositories, candidates = wanted[publication]
                if label == "LinkCandidate":
                    return pair[1] in candidates
                repository = self.repository_ids.get(pair[1], pair[1])
                repository = self.aliases["Repository"].get(repository, repository)
                return repository in repositories or pair[1] in repositories

            removed += self._retain_edges(("Publication", "MENTIONS_LINK", label), keep)
        return removed

    def promote_link_candidates_batch(self, candidates) -> None:
        # A promoted candidate is removed by the publish itself.
        self.folding.update(candidate for candidate, _url in candidates)


def _all_rows(mongo_db: Database) -> dict[str, list[dict]]:
    """Read prepared rows across all groups.

    A group-scoped comparison would mark other groups' graph data as stale.
    """
    rows: dict[str, list[dict]] = {}
    for entity, filename in ENTITY_FILES.items():
        collection = mongo_db[PreparedStore.COLLECTIONS[entity]]
        rows[filename] = list(collection.find(
            {}, {"_id": False, "groups": False, "_version": False}))
    return rows


def _rows_by_label(rows_by_file: dict[str, list[dict]]) -> dict[str, set[str]]:
    """Keep ids with source rows, including failed enrichment skipped by the loader."""
    known: dict[str, set[str]] = defaultdict(set)
    for filename, label in FILE_LABELS.items():
        known[label].update(row["id"] for row in rows_by_file.get(filename) or () if row.get("id"))
    return known


def _claimed(mongo_db: Database) -> tuple[dict[str, set[str]], set[tuple[str, str, str, str, str]]]:
    """What a person added by hand and said they wanted kept."""
    nodes: dict[str, set[str]] = defaultdict(set)
    edges: set[tuple[str, str, str, str, str]] = set()
    for row in mongo_db["graph_overrides"].find({"active": True}):
        if row.get("kind") == "rel":
            if row.get("op") == LINK:
                edges.add((row["src_label"], row["rel_type"], row["tgt_label"],
                           row["src_id"], row["target_id"]))
        elif row.get("op") != DELETE:
            # Any decision about a record is a reason to keep it, not only "create".
            nodes[row["label"]].add(row["target_id"])
    return nodes, edges


def _through_folds(edges: set[tuple[str, str]], source_map: dict[str, str],
                   target_map: dict[str, str]) -> set[tuple[str, str]]:
    """Include edges both before and after graph folds.

    Leave target_map empty for targets matched by URL or login rather than id.
    """
    return edges | {(source_map.get(src, src), target_map.get(tgt, tgt)) for src, tgt in edges}


def plan(client, mongo_db: Database) -> Plan:
    """Plan stale node and edge removals without changing the graph.

    The result also counts manual claims and pending folds excluded from removal.
    """
    rows_by_file = _drop_tombstoned(_all_rows(mongo_db), mongo_db)
    would = _WouldWrite(client, rows_by_file)
    load_prepared_rows(would, rows_by_file, tombstoned_relationships(mongo_db),
                       tombstoned_ids(mongo_db, "LinkCandidate"))
    known = _rows_by_label(rows_by_file)
    claimed_nodes, claimed_edges = _claimed(mongo_db)
    # A fold moves edges onto the survivor, and no row asks for them there.
    aliases = {label: client.fetch_merged_id_map(label)
               for label in ("Person", "Publication", "Repository")}

    result = Plan(folding=len(would.folding))
    # LinkCandidate has no file of its own: only the replay knows it.
    for label in sorted({*FILE_LABELS.values(), "LinkCandidate"}):
        live = client.fetch_node_ids(label)
        stale = live - known.get(label, set()) - would.nodes.get(label, set()) - would.folding
        result.kept_by_hand += len(stale & claimed_nodes.get(label, set()))
        stale -= claimed_nodes.get(label, set())
        if stale:
            result.nodes[label] = sorted(stale)

    for (src_label, rel_type, tgt_label), match_prop in sorted(RELATIONSHIPS.items()):
        triple = (src_label, rel_type, tgt_label)
        live = client.fetch_relationship_pairs(src_label, rel_type, tgt_label, match_prop)
        stale = live - _through_folds(would.edges.get(triple, set()),
                                      aliases.get(src_label, {}),
                                      aliases.get(tgt_label, {}) if match_prop == "id" else {})
        claimed_pairs = {(src_id, tgt_id) for src, rel, tgt, src_id, tgt_id in claimed_edges
                         if (src, rel, tgt) == triple}
        by_hand = stale & _through_folds(claimed_pairs, aliases.get(src_label, {}),
                                        aliases.get(tgt_label, {}) if match_prop == "id" else {})
        result.kept_by_hand += len(by_hand)
        stale -= by_hand
        if stale:
            result.edges[triple] = sorted(stale)
    return result


def apply(client, plan_to_apply: Plan) -> dict[str, int]:
    """Apply planned removals and return deletion counts.

    Delete edges first so node deletion cannot remove edges absent from the plan.
    Pass an audited client to record each deletion.
    """
    edges = nodes = 0
    for (src_label, rel_type, tgt_label), pairs in plan_to_apply.edges.items():
        match_prop = RELATIONSHIPS[(src_label, rel_type, tgt_label)]
        for chunk in chunked(list(pairs)):
            edges += client.delete_relationships_batch(
                src_label, tgt_label, rel_type, chunk, match_prop)
    for label, ids in plan_to_apply.nodes.items():
        for chunk in chunked(list(ids)):
            nodes += client.delete_nodes_batch(label, chunk, detach=True)
    logger.info("prune: %d edge(s) and %d node(s) removed", edges, nodes)
    return {"pruned_relationships": edges, "pruned_nodes": nodes}


def run(client, mongo_db: Database, apply_it: bool = False,
        report: Callable[[str], None] | None = None) -> Plan:
    """Plan and optionally apply a prune while holding both resource locks.

    The locks cover both steps so the plan cannot become stale between them.
    By default, apply_it=False only reports the plan.

    Raises:
        Busy: Another run holds the graph or GitHub matching lock.
    """
    with held(mongo_db, GRAPH), held(mongo_db, GITHUB_MATCH):
        planned = plan(client, mongo_db)
        if apply_it:
            if report is not None:
                report("чистка графа")
            planned.removed = apply(client, planned)
        return planned
