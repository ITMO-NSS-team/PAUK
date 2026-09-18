"""Removing from the graph what the source no longer holds.

Publishing only ever adds. Every node and every edge goes in through
`MERGE`, and nothing has ever taken one out because a prepared row stopped
asking for it. MongoDB does the opposite — a row no group claims any more
is deleted — so the two drift apart, silently and in one direction: the
graph keeps everything it was ever told, including claims a later repair
withdrew.

What "should be there" is not worked out a second time here. The loader is
replayed against a stand-in that writes nothing and remembers what it was
asked to write, so the answer is the loader's own by construction. A prune
built on a parallel reading of the rows would differ from a publish the
first time either changed.

Three things are kept that the rows do not explain, and each for its own
reason:

- what a person added by hand, which is claimed in `graph_overrides` —
  that claim exists for this and nothing else;
- what the next publish will fold rather than delete, because its row was
  merged into another one and its edges have to move, not disappear;
- any record whose row still exists but which the loader skipped this time
  round, a repository whose enrichment failed being the usual case.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from pymongo.database import Database

from pauk.jobs.locks import held
from pauk.jobs.models import GRAPH
from pauk.storage.prepared import PreparedStore

from .client import chunked
from .jsonl_loader import load_prepared_rows
from .load import ENTITY_FILES, FILE_LABELS, _drop_tombstoned
from .mutations import RELATIONSHIPS
from .overrides import DELETE, LINK, tombstoned_ids, tombstoned_relationships

logger = logging.getLogger(__name__)

#: A relationship as both sides of the comparison name it: the triple, and
#: the pairs of (source id, whatever addresses the far end).
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
    """A client that remembers what a publish would write, and writes nothing.

    Reads pass through to the real client, because the loader asks it real
    questions on the way — which ids are already folded, above all.
    """

    def __init__(self, client) -> None:
        self._client = client
        self.nodes: dict[str, set[str]] = defaultdict(set)
        self.edges: dict[Triple, set[tuple[str, str]]] = defaultdict(set)
        #: Ids a publish would fold into another record. Not expected to
        #: exist afterwards, and not stale either: deleting one would take
        #: its edges with it instead of moving them.
        self.folding: set[str] = set()

    def __getattr__(self, name: str):
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

    def _fold(self, merges: list[tuple[str, str]]) -> int:
        self.folding.update(duplicate for duplicate, _canonical in merges)
        return 0

    def merge_person_nodes_batch(self, merges) -> int:
        return self._fold(merges)

    def merge_publication_nodes_batch(self, merges) -> int:
        return self._fold(merges)

    def merge_repository_nodes_batch(self, merges) -> int:
        return self._fold(merges)

    def promote_link_candidates_batch(self, candidates) -> None:
        # A candidate that turned into a repository is removed by the
        # publish itself, which is not this one's business either.
        self.folding.update(candidate for candidate, _url in candidates)


def _all_rows(mongo_db: Database) -> dict[str, list[dict]]:
    """Every prepared row there is, keyed the way the loader wants them.

    Not one group. The graph is the union of every group ever published, so
    a comparison scoped to one of them would call another group's work
    stale.
    """
    rows: dict[str, list[dict]] = {}
    for entity, filename in ENTITY_FILES.items():
        collection = mongo_db[PreparedStore.COLLECTIONS[entity]]
        rows[filename] = list(collection.find(
            {}, {"_id": False, "groups": False, "_version": False}))
    return rows


def _rows_by_label(rows_by_file: dict[str, list[dict]]) -> dict[str, set[str]]:
    """Ids the source still holds, per node label.

    The floor under the whole comparison. A record whose row exists is
    never stale, even when this publish would not write it — a repository
    whose enrichment failed is skipped by the loader and would otherwise
    look like a leftover.
    """
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
            # Any decision about a record is a reason to keep it, not only
            # a "create": somebody editing a field of a record no row
            # explains is saying the same thing about it.
            nodes[row["label"]].add(row["target_id"])
    return nodes, edges


def _through_folds(edges: set[tuple[str, str]], source_map: dict[str, str],
                   target_map: dict[str, str]) -> set[tuple[str, str]]:
    """The edges a row asks for, plus where a fold has since put them.

    Both forms are expected, not only the folded one: the same pair of rows
    can be published to a graph where the fold has happened and to one
    where it has not yet.

    Args:
        target_map: Empty unless the far end is addressed by its id. An
            alias map is keyed by id, and a Repository is matched on its
            url — translating one with the other would find nothing and
            quietly drop the edge from what is expected.
    """
    return edges | {(source_map.get(src, src), target_map.get(tgt, tgt)) for src, tgt in edges}


def plan(client, mongo_db: Database) -> Plan:
    """What the graph holds that the source no longer asks for.

    Args:
        client: Graph client. Reads only — planning changes nothing.
        mongo_db: Mongo database holding the prepared rows and the manual
            decisions.

    Returns:
        The nodes and edges a prune would remove, and how many it left
        alone because somebody claimed them or because the next publish
        will fold them.
    """
    rows_by_file = _drop_tombstoned(_all_rows(mongo_db), mongo_db)
    would = _WouldWrite(client)
    load_prepared_rows(would, rows_by_file, tombstoned_relationships(mongo_db),
                       tombstoned_ids(mongo_db, "LinkCandidate"))
    known = _rows_by_label(rows_by_file)
    claimed_nodes, claimed_edges = _claimed(mongo_db)
    # A fold moves edges onto the survivor and the rows know nothing about
    # it: after A2 is folded into A1, the edge to A2's work hangs off A1,
    # and only A2's row asks for it. Read as it stands, that edge has no
    # row behind it, and a prune would undo every fold the graph has made.
    aliases = {label: client.fetch_merged_id_map(label)
               for label in ("Person", "Publication", "Repository")}

    result = Plan(folding=len(would.folding))
    # LinkCandidate has no prepared file of its own — it is invented from
    # repo_links rows — so the replay is the only thing that knows it.
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
        by_hand = {(src_id, tgt_id) for src_id, tgt_id in stale
                   if (src_label, rel_type, tgt_label, src_id, tgt_id) in claimed_edges}
        result.kept_by_hand += len(by_hand)
        stale -= by_hand
        if stale:
            result.edges[triple] = sorted(stale)
    return result


def apply(client, plan_to_apply: Plan) -> dict[str, int]:
    """Remove what the plan names.

    Edges first, then nodes: a node is deleted with whatever is still
    attached to it, and taking the edges away first keeps that from being
    the thing that removes an edge nobody counted.

    Args:
        client: Graph client; pass the audited one. A deletion is always
            recorded row by row, which makes the journal the record of what
            was removed.

    Returns:
        How many edges and nodes were actually removed.
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
    """Compare, and remove what the comparison found if that is what was asked.

    One lock around both halves rather than one around each: between a plan
    and its application the graph must not move, or the plan describes a
    graph that is no longer there. Held here and not by the caller, the way
    a publish holds it, so a run from a terminal and a run from the panel
    take the same turn.

    Args:
        apply_it: False compares and reports, and is what both callers do
            by default.
        report: Told when the removal starts, for a run that has somewhere
            to say it.

    Raises:
        Busy: Something else is writing the graph.
    """
    with held(mongo_db, GRAPH):
        planned = plan(client, mongo_db)
        if apply_it:
            if report is not None:
                report("чистка графа")
            planned.removed = apply(client, planned)
        return planned
