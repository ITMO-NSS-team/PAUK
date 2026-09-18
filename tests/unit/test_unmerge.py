"""Taking a fold apart again.

Every test here folds two people the way the panel folds them — through
`merge_nodes`, against a double that moves edges and fills fields the way
the real client does — and then asks for it back. Building the fold by hand
would prove nothing: what has to come back is exactly what a real merge
takes away.
"""

import unittest

import mongomock

from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import MutationError, NotFound, merge_nodes
from pauk.graph.overrides import record_override, record_relationship_override
from pauk.graph.unmerge import NothingToRebuild, split_person
from pauk.models import Person, Publication
from pauk.storage import PreparedStore
from tests.unit.test_admin_nodes import FakePanelGraph

GROUP = "sample"


def person(pid, name, works=(), *, is_itmo=False, **fields):
    return Person(id=pid, openalex_id=pid, name_raw=name, is_itmo=is_itmo,
                  authored=[{"publication_id": work, "position": 1} for work in works],
                  **fields)


class UnmergeTest(unittest.TestCase):
    """The common setup: two people, one publication each, then a fold."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, GROUP)
        self.graph = FakePanelGraph()

    def publish(self, *people, publications=("W1", "W2")):
        """Write the rows and load them, the way `pauk publish graph` does."""
        self.prepared.write_models("persons", list(people))
        self.prepared.write_models("publications", [
            Publication(id=pid, title=pid) for pid in publications])
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()})

    def fold(self, duplicate="A2", canonical="A1"):
        merge_nodes(self.graph, "Person", duplicate, canonical)

    def edges(self, node_id):
        return {(rel_type, tgt_id) for _src_label, rel_type, _tgt_label, src_id, tgt_id
                in self.graph.relationships if src_id == node_id}


class WhatComesBackTest(UnmergeTest):
    def setUp(self):
        super().setUp()
        self.publish(person("A1", "Ivan Smirnov", works=["W1"], is_itmo=True),
                     person("A2", "I. Smirnov", works=["W2"], orcid="0000-0002"))
        self.fold()

    def test_the_fold_is_what_this_starts_from(self):
        # Guards the setup, not the code: every test below reads as an undo
        # only if the pair really was folded first.
        self.assertNotIn(("Person", "A2"), self.graph.nodes)
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1"), ("AUTHORED", "W2")})

    def test_the_record_is_a_node_again(self):
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertIn(("Person", "A2"), self.graph.nodes)
        self.assertEqual(self.graph.nodes[("Person", "A2")]["name_raw"], "I. Smirnov")

    def test_its_work_comes_back_with_it(self):
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W2")})

    def test_and_leaves_the_survivor(self):
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1")})

    def test_the_survivor_stops_claiming_it(self):
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.graph.nodes[("Person", "A1")].get("merged_ids"), [])
        # That list is not bookkeeping: every publish and every graph-wide
        # dedup reads it back as an alias map and folds by it. Left as it
        # was, it would put the pair back together on its own.
        self.assertEqual(self.graph.fetch_merged_id_map("Person"), {})

    def test_what_the_fold_borrowed_goes_back(self):
        # The survivor had no ORCID and took the duplicate's. Left behind it
        # is not just stale: it is the identity of somebody else, and the
        # rules would fold the two again on it.
        self.assertEqual(self.graph.nodes[("Person", "A1")]["orcid"], "0000-0002")
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertIsNone(self.graph.nodes[("Person", "A1")].get("orcid"))
        self.assertEqual(self.graph.nodes[("Person", "A2")]["orcid"], "0000-0002")

    def test_a_field_of_its_own_is_left_alone(self):
        # is_itmo is the survivor's, not something the fold brought in.
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertTrue(self.graph.nodes[("Person", "A1")]["is_itmo"])

    def test_a_publish_finds_nothing_left_to_repair(self):
        # An undo has to leave the graph in the state a publish would build
        # from the same rows, or the next run quietly corrects it into
        # something else.
        split_person(self.graph, self.db, ["A1", "A2"])
        before = dict(self.graph.relationships)
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()})
        self.assertIn(("Person", "A2"), self.graph.nodes)
        self.assertEqual(set(self.graph.relationships), set(before))

    def test_it_says_what_it_did(self):
        result = split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(result["duplicate"], "A2")
        self.assertEqual(result["canonical"], "A1")
        self.assertEqual((result["given_back"], result["taken_off"]), (1, 1))


class SharedWorkTest(UnmergeTest):
    """An edge both of them claim belongs to both of them."""

    def test_the_survivors_own_edge_survives(self):
        self.publish(person("A1", "Ivan Smirnov", works=["W1"]),
                     person("A2", "I. Smirnov", works=["W1", "W2"]))
        self.fold()
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1")})
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W1"), ("AUTHORED", "W2")})


class HandWorkTest(UnmergeTest):
    """What people did by hand is not collateral damage."""

    def setUp(self):
        super().setUp()
        self.publish(person("A1", "Ivan Smirnov", works=["W1"]),
                     person("A2", "I. Smirnov", works=["W2"]))

    def test_an_unlinked_edge_is_never_offered_back(self):
        # Not just absent at the end — `apply_overrides` would delete it
        # again anyway. It must not be created in the first place, or every
        # undo writes a creation and a deletion into the journal.
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication",
                                     "A2", "W2", actor="user:roman")
        self.fold()
        asked = []
        upsert = self.graph.upsert_relationships_batch

        def watched(src_label, tgt_label, rel_type, rels, tgt_match_prop="id"):
            asked.extend((src_id, tgt_id) for src_id, tgt_id, _props in rels)
            return upsert(src_label, tgt_label, rel_type, rels, tgt_match_prop)

        self.graph.upsert_relationships_batch = watched
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertNotIn(("A2", "W2"), asked)
        self.assertEqual(self.edges("A2"), set())

    def test_a_corrected_field_is_put_back_on_top(self):
        # The survivor's row says one thing and a person said another. The
        # rebuild reads the row, so without the reapply at the end the
        # correction would be buried by the undo.
        record_override(self.db, "Person", "A1", "set",
                        {"name_raw": "Смирнов Иван"}, actor="user:roman")
        self.fold()
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_raw"], "Смирнов Иван")

    def test_an_edge_nobody_claims_stays_where_it_is(self):
        # Added by hand, recorded nowhere: which of the two it was put on is
        # not knowable, so it is left rather than guessed at.
        self.fold()
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W3")] = {}
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertIn(("AUTHORED", "W3"), self.edges("A1"))


class RefusalTest(UnmergeTest):
    def test_a_pair_that_was_never_folded(self):
        self.publish(person("A1", "Ivan Smirnov"), person("A2", "I. Smirnov"))
        with self.assertRaises(MutationError):
            split_person(self.graph, self.db, ["A1", "A2"])

    def test_a_fold_that_happened_somewhere_else(self):
        # A2 is gone, but not into A1 — undoing "the fold" would be undoing
        # one that never happened.
        self.publish(person("A1", "Ivan Smirnov"), person("A2", "I. Smirnov"),
                     person("A3", "Ivan S."))
        merge_nodes(self.graph, "Person", "A2", "A3")
        with self.assertRaises(NotFound):
            split_person(self.graph, self.db, ["A1", "A2"])

    def test_a_record_with_no_row_left(self):
        # What the collection stage's own dedup leaves behind: it deletes
        # the rows it folds, so there is nothing to rebuild the node from.
        self.publish(person("A1", "Ivan Smirnov"), person("A2", "I. Smirnov"))
        self.fold()
        self.db.persons.delete_one({"id": "A2"})
        with self.assertRaises(NothingToRebuild):
            split_person(self.graph, self.db, ["A1", "A2"])

    def test_a_record_somebody_deleted_by_hand(self):
        self.publish(person("A1", "Ivan Smirnov"), person("A2", "I. Smirnov"))
        self.fold()
        record_override(self.db, "Person", "A2", "delete", actor="user:roman")
        with self.assertRaises(NothingToRebuild):
            split_person(self.graph, self.db, ["A1", "A2"])

    def test_nothing_is_half_done_by_a_refusal(self):
        self.publish(person("A1", "Ivan Smirnov", works=["W1"]),
                     person("A2", "I. Smirnov", works=["W2"]))
        self.fold()
        self.db.persons.delete_one({"id": "A2"})
        with self.assertRaises(NothingToRebuild):
            split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["merged_ids"], ["A2"])
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1"), ("AUTHORED", "W2")})


class SurvivorWithNoRowTest(UnmergeTest):
    """The survivor's own row can be gone too, folded away by a later run.

    Then there is nothing to restore its fields from, and guessing is worse
    than leaving them: what the fold added and what was always the
    survivor's own look exactly alike.
    """

    def setUp(self):
        super().setUp()
        self.publish(person("A1", "Ivan Smirnov", works=["W1"]),
                     person("A2", "I. Smirnov", works=["W2"], orcid="0000-0002"))
        self.fold()
        self.db.persons.delete_one({"id": "A1"})

    def test_the_record_still_comes_back(self):
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertIn(("Person", "A2"), self.graph.nodes)
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W2")})

    def test_the_survivor_keeps_its_own_fields(self):
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_raw"], "Ivan Smirnov")

    def test_but_stops_claiming_the_record(self):
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["merged_ids"], [])


class ChainTest(UnmergeTest):
    """A record that had swallowed somebody else before it was swallowed."""

    def test_what_it_had_taken_leaves_with_it(self):
        self.publish(person("A1", "Ivan Smirnov"),
                     person("A2", "I. Smirnov", merged_ids=["A9"]))
        self.fold()
        self.assertEqual(sorted(self.graph.nodes[("Person", "A1")]["merged_ids"]), ["A2", "A9"])
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["merged_ids"], [])
        self.assertEqual(self.graph.nodes[("Person", "A2")]["merged_ids"], ["A9"])
