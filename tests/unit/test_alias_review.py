import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mongomock

from pauk.graph.alias_review import record_alias_conflict
from pauk.graph.client import MergeAliasConflict, Neo4jClient, flatten_merges
from pauk.graph.dedup import _dedup_locked
from pauk.graph.load import _load_locked
from pauk.settings import Settings
from pauk.storage import PreparedStore, review
from tests.bench.mocks import RecordingNeo4jClient


class AliasReviewTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db

    def conflict(self, label="Person"):
        try:
            flatten_merges([("A", "B"), ("A", "C"), ("X", "Y")], label=label)
        except MergeAliasConflict as error:
            return error
        self.fail("Expected ambiguous aliases")

    def test_conflict_retains_only_the_affected_component(self):
        conflict = self.conflict()
        self.assertEqual(conflict.members, ["A", "B", "C"])
        self.assertEqual(conflict.aliases, [("A", "B"), ("A", "C")])
        self.assertEqual(conflict.label, "Person")
        self.assertIsInstance(conflict, ValueError)

    def test_cycle_is_structured_and_safe_diamond_still_flattens(self):
        with self.assertRaises(MergeAliasConflict) as caught:
            flatten_merges([("A", "B"), ("B", "A"), ("X", "Y")], label="Person")
        self.assertEqual(caught.exception.members, ["A", "B"])
        self.assertEqual(dict(flatten_merges([("A", "B"), ("A", "C"), ("B", "C")])),
                         {"A": "C", "B": "C"})

    def test_repeated_conflict_is_one_review_without_overwriting_human_verdict(self):
        review.record_verdict(self.db, review.GROUP, ["A", "B", "C"], review.DIFFERENT, actor="tester")
        for _ in range(2):
            record_alias_conflict(self.db, self.conflict(), "publish:test")
        question = self.db.review_pairs.find_one({"kind": review.ALIAS})
        self.assertEqual(self.db.review_pairs.count_documents({}), 2)
        self.assertEqual(question["members"], ["A", "B", "C"])
        self.assertEqual(question["evidence"]["route"], "merge_alias_conflict")
        self.assertEqual(question["evidence"]["operation"], "publish:test")
        self.assertTrue(question["evidence"]["requires_alias_repair"])
        record_alias_conflict(self.db, self.conflict(), "publish:test")
        self.assertEqual(self.db.review_pairs.find_one({"kind": review.GROUP})["verdict"], review.DIFFERENT)
        self.assertEqual(review.count(self.db, pressing=True, answered=False, skipped=False), 1)

    def test_group_refresh_and_skip_do_not_hide_or_overwrite_alias_incident(self):
        ordinary = {"status": "held", "persons": ["A", "B", "C"], "held_because": ["old reason"]}
        review.record_held(self.db, [ordinary])
        review.skip(self.db, review.GROUP, ["A", "B", "C"])
        record_alias_conflict(self.db, self.conflict(), "publish:test")
        review.record_held(self.db, [ordinary])
        question = self.db.review_pairs.find_one({"kind": review.ALIAS})
        self.assertEqual(question["evidence"]["alias_edges"], [
            {"alias": "A", "target": "B"}, {"alias": "A", "target": "C"}])
        self.assertEqual(review.count(self.db, pressing=True, answered=False, skipped=False), 1)

    def test_alias_diagnostic_is_not_an_identity_answer_even_with_explicit_kinds(self):
        record_alias_conflict(self.db, self.conflict(), "publish:test")
        self.db.review_pairs.update_one({"kind": review.ALIAS}, {"$set": {"verdict": review.SAME}})
        self.assertEqual(review.decisions(self.db, kinds=review.KINDS), {})

    def test_alias_and_disguised_group_actions_cannot_write_decisions(self):
        record_alias_conflict(self.db, self.conflict(), "publish:test")
        for kind, members in ((review.ALIAS, ["A", "B", "C"]),
                              (review.GROUP, ["A", "B", "C"]), (review.PAIR, ["A", "B"])):
            for action, args in ((review.record_verdict, (self.db, kind, members, review.DIFFERENT)),
                                 (review.skip, (self.db, kind, members)),
                                 (review.withdraw, (self.db, kind, members))):
                with self.subTest(kind=kind), self.assertRaises(review.ReviewError):
                    action(*args)
        with self.assertRaises(review.ReviewError):
            review.record_split(self.db, ["A", "B", "C"], ["A", "B"])
        self.assertEqual(review.decisions(self.db), {})
        self.assertEqual(review.count(self.db, answered=False, skipped=False), 1)

    def test_expanded_group_split_checks_every_pair_before_writing(self):
        record_alias_conflict(self.db, self.conflict(), "publish:test")
        with self.assertRaises(review.ReviewError):
            review.record_split(self.db, ["A", "B", "C", "X"], ["A", "X"])
        self.assertEqual(review.decisions(self.db), {})
        self.assertEqual(self.db.review_pairs.count_documents({}), 1)

    def test_non_person_conflict_does_not_create_a_person_question(self):
        record_alias_conflict(self.db, self.conflict("Publication"), "publish:test")
        self.assertEqual(self.db.review_pairs.count_documents({}), 0)

    def test_publish_queues_conflict_before_any_data_writes_and_closes_client(self):
        graph = RecordingNeo4jClient()
        graph.close = Mock()
        PreparedStore(self.db, "test").write_rows("persons", [
            {"id": "B", "merged_ids": ["A"]},
            {"id": "C", "merged_ids": ["A"]},
        ])
        with patch("pauk.graph.load.audited_client", return_value=graph), \
                patch("pauk.graph.load.create_constraints"), \
                patch("pauk.graph.load.apply_overrides") as overrides, \
                self.assertRaises(MergeAliasConflict):
            _load_locked(Settings(), self.db, "test")
        self.assertFalse(any(graph.nodes.values()))
        self.assertFalse(graph.edges)
        graph.close.assert_called_once()
        overrides.assert_not_called()
        self.assertEqual(self.db.review_pairs.count_documents({}), 1)

    def test_conflict_between_published_and_prepared_aliases_is_queued(self):
        graph = RecordingNeo4jClient()
        graph.close = Mock()
        graph.upsert_nodes_batch("Person", [("B", {"id": "B", "merged_ids": ["A"]})])
        PreparedStore(self.db, "test").write_rows("persons", [{"id": "C", "merged_ids": ["A"]}])
        with patch("pauk.graph.load.audited_client", return_value=graph), \
                patch("pauk.graph.load.create_constraints"), self.assertRaises(MergeAliasConflict):
            _load_locked(Settings(), self.db, "test")
        self.assertEqual(set(graph.nodes["Person"]), {"B"})
        self.assertEqual(graph.nodes["Person"]["B"]["merged_ids"], ["A"])
        self.assertEqual(self.db.review_pairs.find_one()["members"], ["A", "B", "C"])

    def test_graph_dedup_queues_historical_conflict_before_planning(self):
        graph = Mock()
        graph.fetch_merged_id_map.side_effect = self.conflict()
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            with patch("pauk.graph.dedup.audited_client", return_value=graph), \
                    patch("pauk.graph.dedup.dedup_graph_persons") as planner, \
                    self.assertRaises(MergeAliasConflict):
                _dedup_locked(config, self.db)
        planner.assert_not_called()
        graph.close.assert_called_once()
        self.assertEqual(self.db.review_pairs.find_one()["evidence"]["operation"], "dedup-graph")

    def test_real_client_preserves_label_on_historical_conflict(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = Mock()
        session = Mock()
        client.driver.session.return_value.__enter__ = Mock(return_value=session)
        client.driver.session.return_value.__exit__ = Mock(return_value=False)
        tx = Mock()
        tx.run.return_value = [{"merged_id": "A", "canonical_id": target} for target in ("B", "C")]
        session.execute_read.side_effect = lambda callback: callback(tx)
        with self.assertRaises(MergeAliasConflict) as caught:
            client.fetch_merged_id_map("Person")
        self.assertEqual(caught.exception.label, "Person")
