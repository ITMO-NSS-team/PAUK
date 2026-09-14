"""Bringing the graph back to what the prepared rows describe.

Every test here publishes for real first, then changes the source the way
a repair or a later run changes it, and asks what the graph is now holding
that nothing asks for. Building the "before" by hand would test the
comparison against itself.
"""

import unittest
from unittest.mock import patch

import mongomock

from pauk.graph import prune
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import merge_nodes
from pauk.graph.overrides import record_override, record_relationship_override
from pauk.jobs import locks
from pauk.jobs.models import PrunePayload
from pauk.jobs.worker import _prune
from pauk.models import GitHubProfile, Person, Publication, Repository
from pauk.settings import Settings
from pauk.storage import PreparedStore
from tests.unit.test_admin_nodes import FakePanelGraph

GROUP = "sample"


def person(pid, name, works=(), **fields):
    return Person(id=pid, openalex_id=pid, name_raw=name, is_itmo=True,
                  authored=[{"publication_id": work, "position": 1} for work in works],
                  **fields)


def repository(rid, url, publications=()):
    return Repository(id=rid, name=rid, url=url, cited_urls=[url],
                      publication_ids=list(publications),
                      processing={"repositories": {"status": "completed", "attempts": 1}})


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, GROUP)
        self.graph = FakePanelGraph()

    def write(self, **entities):
        for entity, rows in entities.items():
            self.prepared.write_models(entity, rows)

    def publish(self):
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()})

    def plan(self):
        return prune.plan(self.graph, self.db)

    def people(self):
        return sorted(node_id for label, node_id in self.graph.nodes if label == "Person")

    def links(self, rel_type):
        return sorted((src, tgt) for _s, rel, _t, src, tgt
                      in self.graph.relationships if rel == rel_type)


class NothingToDoTest(PruneTest):
    def test_a_graph_that_matches_its_rows_is_left_alone(self):
        self.write(persons=[person("A1", "Ivan", ["W1"])],
                   publications=[Publication(id="W1", title="One")])
        self.publish()
        plan = self.plan()
        self.assertEqual((plan.nodes, plan.edges), ({}, {}))
        self.assertEqual(plan.total(), 0)


class RetractedClaimTest(PruneTest):
    """The case the issue was opened for: a claim a repair took away."""

    def setUp(self):
        super().setUp()
        self.write(publications=[Publication(id="W1", title="One")],
                   repositories=[repository("R1", "https://github.com/x/r", ["W1"])])
        self.publish()

    def test_the_link_is_there_to_begin_with(self):
        self.assertEqual(self.links("IMPLEMENTS"), [("R1", "W1")])

    def test_dropping_it_from_the_row_makes_it_stale(self):
        self.write(repositories=[repository("R1", "https://github.com/x/r")])
        plan = self.plan()
        self.assertEqual(plan.edges,
                         {("Repository", "IMPLEMENTS", "Publication"): [("R1", "W1")]})
        self.assertEqual(plan.nodes, {})

    def test_and_applying_removes_it(self):
        self.write(repositories=[repository("R1", "https://github.com/x/r")])
        result = prune.apply(self.graph, self.plan())
        self.assertEqual(result["pruned_relationships"], 1)
        self.assertEqual(self.links("IMPLEMENTS"), [])
        # The repository itself stays: its row is still there.
        self.assertIn(("Repository", "R1"), self.graph.nodes)

    def test_a_publish_alone_would_not_have(self):
        # What makes the prune necessary rather than a nicety.
        self.write(repositories=[repository("R1", "https://github.com/x/r")])
        self.publish()
        self.assertEqual(self.links("IMPLEMENTS"), [("R1", "W1")])


class DeletedRowTest(PruneTest):
    """A record no group claims any more is deleted from Mongo, not the graph."""

    def setUp(self):
        super().setUp()
        self.write(github_profiles=[
            GitHubProfile(id="G1", login="one"), GitHubProfile(id="G2", login="two")])
        self.publish()

    def test_the_record_left_behind_is_found(self):
        self.write(github_profiles=[GitHubProfile(id="G1", login="one")])
        self.assertEqual(self.plan().nodes, {"GitHubProfile": ["G2"]})

    def test_and_removed(self):
        self.write(github_profiles=[GitHubProfile(id="G1", login="one")])
        prune.apply(self.graph, self.plan())
        self.assertEqual(sorted(node_id for label, node_id in self.graph.nodes
                                if label == "GitHubProfile"), ["G1"])


class HandMadeTest(PruneTest):
    """What a person added is not a leftover, and the claim is how it says so."""

    def setUp(self):
        super().setUp()
        self.write(persons=[person("A1", "Ivan")],
                   publications=[Publication(id="W1", title="One")])
        self.publish()

    def test_a_record_somebody_claimed_is_kept(self):
        self.graph.add("Person", "A9", name_ru="Заведён вручную")
        record_override(self.db, "Person", "A9", "create", {"name_ru": "Заведён вручную"},
                        actor="user:roman")
        plan = self.plan()
        self.assertEqual(plan.nodes, {})
        self.assertEqual(plan.kept_by_hand, 1)

    def test_the_same_record_unclaimed_is_a_leftover(self):
        # The claim is doing the work, not the shape of the record.
        self.graph.add("Person", "A9", name_ru="Ниоткуда")
        self.assertEqual(self.plan().nodes, {"Person": ["A9"]})

    def test_a_link_somebody_claimed_is_kept(self):
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")] = {}
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication", "A1", "W1",
                                     op="link", actor="user:roman")
        plan = self.plan()
        self.assertEqual(plan.edges, {})
        self.assertEqual(plan.kept_by_hand, 1)

    def test_the_same_link_unclaimed_is_a_leftover(self):
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")] = {}
        self.assertEqual(self.plan().edges,
                         {("Person", "AUTHORED", "Publication"): [("A1", "W1")]})

    def test_an_edited_record_is_kept_too(self):
        # Editing a field of a record no row explains says the same thing
        # about it as creating it did.
        self.graph.add("Person", "A9", name_ru="Правленый")
        record_override(self.db, "Person", "A9", "set", {"name_ru": "Правленый"},
                        actor="user:roman")
        self.assertEqual(self.plan().nodes, {})


class FoldedRecordTest(PruneTest):
    """A record folded into another one is moved, not dropped."""

    def test_a_row_folded_by_the_stage_is_left_to_the_publish(self):
        # The stage deletes the row it folds and writes the id onto the
        # survivor. Until the next publish the node is still there, and
        # deleting it would take its work with it instead of moving it.
        self.write(persons=[person("A1", "Ivan", ["W1"]), person("A2", "I. S.", ["W2"])],
                   publications=[Publication(id="W1", title="One"),
                                 Publication(id="W2", title="Two")])
        self.publish()
        self.write(persons=[person("A1", "Ivan", ["W1"], merged_ids=["A2"])])
        plan = self.plan()
        self.assertEqual(plan.nodes, {})
        self.assertEqual(plan.folding, 1)

    def test_and_the_publish_does_move_it(self):
        self.write(persons=[person("A1", "Ivan", ["W1"]), person("A2", "I. S.", ["W2"])],
                   publications=[Publication(id="W1", title="One"),
                                 Publication(id="W2", title="Two")])
        self.publish()
        self.write(persons=[person("A1", "Ivan", ["W1"], merged_ids=["A2"])])
        self.publish()
        self.assertEqual(self.people(), ["A1"])
        self.assertEqual(self.links("AUTHORED"), [("A1", "W1"), ("A1", "W2")])

    def test_a_pair_folded_in_the_graph_leaves_no_leftover(self):
        # Both rows are still there, one node is gone. Nothing to prune,
        # and in particular the survivor is not called stale.
        self.write(persons=[person("A1", "Ivan", ["W1"]), person("A2", "I. S.", ["W2"])],
                   publications=[Publication(id="W1", title="One"),
                                 Publication(id="W2", title="Two")])
        self.publish()
        merge_nodes(self.graph, "Person", "A2", "A1")
        plan = self.plan()
        self.assertEqual(plan.nodes, {})
        # A1 now owns A2's work, which no row of A1 asks for — but A2's row
        # does, and the loader moves it there on every publish.
        self.assertEqual(plan.edges, {})


class SkippedRowTest(PruneTest):
    """A row the loader passes over is not a row that stopped existing."""

    def test_a_repository_whose_enrichment_failed_is_kept(self):
        self.write(repositories=[repository("R1", "https://github.com/x/r")])
        self.publish()
        failed = Repository(
            id="R1", name="R1", url="https://github.com/x/r",
            cited_urls=["https://github.com/x/r"],
            processing={"repositories": {"status": "failed", "attempts": 3}})
        self.write(repositories=[failed])
        self.assertEqual(self.plan().nodes, {})


class UnlinkedByHandTest(PruneTest):
    """An edge somebody removed is already gone, and stays accounted for."""

    def test_a_tombstoned_edge_is_not_reported_twice(self):
        self.write(persons=[person("A1", "Ivan", ["W1"])],
                   publications=[Publication(id="W1", title="One")])
        self.publish()
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication", "A1", "W1",
                                     actor="user:roman")
        self.graph.relationships.pop(("Person", "AUTHORED", "Publication", "A1", "W1"))
        self.assertEqual(self.plan().edges, {})


class PruneAsAJobTest(PruneTest):
    """The step the panel schedules. Counting and removing are one run."""

    def setUp(self):
        super().setUp()
        self.write(github_profiles=[GitHubProfile(id="G1", login="one"),
                                    GitHubProfile(id="G2", login="two")])
        self.publish()
        self.write(github_profiles=[GitHubProfile(id="G1", login="one")])
        self.told = []

    def run_step(self, apply):
        with patch("pauk.graph.audit.audited_client", return_value=self.graph):
            return _prune(Settings(), self.db, PrunePayload(apply=apply),
                          lambda: False, lambda step, *_: self.told.append(step))

    def profiles(self):
        return sorted(node_id for label, node_id in self.graph.nodes
                      if label == "GitHubProfile")

    def test_counting_changes_nothing(self):
        result = self.run_step(apply=False)
        self.assertEqual(result["prune_nodes"], 1)
        self.assertNotIn("pruned_nodes", result)
        self.assertEqual(self.profiles(), ["G1", "G2"])

    def test_applying_removes_what_it_counted(self):
        result = self.run_step(apply=True)
        self.assertEqual((result["prune_nodes"], result["pruned_nodes"]), (1, 1))
        self.assertEqual(self.profiles(), ["G1"])

    def test_it_says_what_it_is_doing(self):
        self.run_step(apply=True)
        self.assertEqual(self.told, ["сверка графа с источником", "чистка графа"])

    def test_a_run_that_only_counts_says_so_by_saying_less(self):
        self.run_step(apply=False)
        self.assertEqual(self.told, ["сверка графа с источником"])

    def test_it_holds_the_graph_while_it_works(self):
        # It deletes nodes and edges. A publish writing at the same time
        # would be comparing against rows this one is about to act on.
        held = []
        real = locks.held

        def watched(db, resource, owner=None):
            held.append(resource)
            return real(db, resource, owner)

        with patch.object(locks, "held", watched):
            self.run_step(apply=False)
        self.assertEqual(held, ["graph"])
