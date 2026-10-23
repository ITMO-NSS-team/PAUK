import unittest
from unittest.mock import MagicMock, patch

import mongomock

from pauk.graph.client import Neo4jClient
from pauk.graph.person_resolution import Decision, ResolverPolicy
from pauk.models import Person
from pauk.person_resolution_defaults import MERGE_FROM, SEPARATE_BELOW
from pauk.pipeline.person_name_blocking import candidate_person_pairs, person_name_forms
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.storage import review


class NameBlockingFollowupTest(unittest.TestCase):
    def test_corpus_frequency_cannot_remove_every_surname(self):
        people = [Person(id=str(i), name_raw=f"Kim Jae{i}", is_member=True) for i in range(100)]
        people += [Person(id="A", name_raw="J. Kim", is_member=True),
                   Person(id="B", name_raw="Jae Kim", is_member=True)]
        forms = person_name_forms(people)
        self.assertTrue(all(forms.values()))
        pairs = {frozenset((a.id, b.id)) for a, b in candidate_person_pairs(people, None)}
        self.assertIn(frozenset(("A", "B")), pairs)

    def test_surname_only_primary_does_not_trust_unrelated_alias(self):
        person = Person(id="A", name_raw="Ivanov", other_names=["Sergey Petrov"], is_member=True)
        self.assertNotIn("petrov", {f.surname for f in person_name_forms([person])["A"]})

    def test_missing_primary_does_not_trust_contradictory_aliases(self):
        person = Person(id="A", other_names=["Sergey Petrov", "Ivan Ivanov"], is_member=True)
        self.assertEqual(person_name_forms([person])["A"], set())

    def test_missing_primary_keeps_consistent_aliases(self):
        person = Person(id="A", other_names=["Sergey Petrov", "Petrov, S."], is_member=True)
        self.assertTrue(any(f.surname == "petrov" for f in person_name_forms([person])["A"]))

    def test_surname_only_primary_accepts_consistent_alias(self):
        person = Person(id="A", name_raw="Ivanov", other_names=["Sergey Ivanov"], is_member=True)
        self.assertTrue(any(f.surname == "ivanov" and f.given == ("sergei",)
                            for f in person_name_forms([person])["A"]))

    def test_exact_identity_survives_absent_names(self):
        people = [Person(id=i, orcid="same", is_member=True) for i in ("A", "B")]
        self.assertEqual(len(list(candidate_person_pairs(people, None))), 1)


class SeparationAuditTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test
        self.held = {"status": "held", "person_a": "A", "person_b": "B", "reason": "uncertain"}
        self.separate = {**self.held, "status": "separate", "route": "logreg_low"}
        review.record_held(self.db, [self.held])

    def test_auto_separate_retires_only_existing_unanswered_question(self):
        review.record_held(self.db, [self.separate, {**self.separate, "person_b": "C"}])
        self.assertEqual(review.count(self.db, answered=False), 0)
        self.assertEqual(review.count(self.db, answered=True), 0)
        self.assertEqual(review.count(self.db), 1)
        self.assertEqual(review.questions(self.db)[0]["auto_resolution"]["route"], "logreg_low")

    def test_new_uncertainty_reopens_an_automatically_retired_question(self):
        review.record_held(self.db, [self.separate])
        review.record_held(self.db, [self.held])
        self.assertEqual(review.count(self.db, answered=False), 1)
        self.assertNotIn("auto_resolution", review.questions(self.db)[0])

    def test_retirement_follows_old_question_aliases(self):
        review.record_held(self.db, [{**self.separate, "person_a": "CurrentA"}], aliases={"A": "CurrentA"})
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_human_answer_is_never_overwritten(self):
        for verdict in review.VERDICTS:
            with self.subTest(verdict=verdict):
                review.record_verdict(self.db, review.PAIR, ["A", "B"], verdict)
                review.record_held(self.db, [self.separate])
                row = review.questions(self.db)[0]
                self.assertEqual(row["verdict"], verdict)
                self.assertNotIn("auto_resolved_at", row)

    def test_deterministic_separations_have_an_audit_row(self):
        people = [Person(id=i, name_raw="Ivan Petrov", is_member=True) for i in ("A", "B")]
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.01):
            groups, report = plan_person_merges_resolved(people, {})
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], Decision.SEPARATE.value)
        self.assertEqual(report[0]["route"], "logreg_low")


class FoldQueryOrderTest(unittest.TestCase):
    def test_both_directions_order_before_collect_and_reverse_before_overlay(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        tx = MagicMock()
        tx.run.return_value.single.return_value = {"removed": 2}
        session = client.driver.session.return_value.__enter__.return_value
        session.execute_write.side_effect = lambda callback: callback(tx)
        for fold in (client.merge_person_nodes_batch, client.merge_publication_nodes_batch):
            tx.reset_mock()
            fold([("A", "B"), ("B", "C")])
            queries = [call.args[0] for call in tx.run.call_args_list if "AS old_edges" in call.args[0]]
            self.assertTrue(queries)
            for query in queries:
                self.assertLess(query.index("ORDER BY priority"), query.index("collect(old)"))
                self.assertIn("FOREACH (old IN reverse(old_edges)", query)
                self.assertIn("SET new += keep", query)
            batch = tx.run.call_args_list[0].kwargs["batch"]
            self.assertEqual(batch[0], {"dup_id": "B", "canonical_id": "C", "priority": 0})

    def test_policy_defaults_are_shared(self):
        self.assertEqual(ResolverPolicy().separate_below, SEPARATE_BELOW)
        self.assertEqual(ResolverPolicy().merge_from, MERGE_FROM)
