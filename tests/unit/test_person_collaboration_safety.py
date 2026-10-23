"""Source-aware guards without changing fitted features or consensus."""

import math
import unittest
from dataclasses import replace

from pauk.graph.person_resolution import (
    Decision,
    ModelVerdict,
    PairEvidence,
    feature_vector,
    first_stage_payload,
    is_multi_person_name,
    resolve_pair,
)
from pauk.models import Person
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.stages.dedup import plan_person_merges
from tests.unit.test_person_resolution_pipeline import FakeModels


class CoauthorModel:
    surname_counts = {}

    def probability(self, features):
        return 0.98 if features["roles_v2__shared_coauthors"] > math.log1p(5) else 0.05


class ConstantModel:
    surname_counts = {}

    def __init__(self, probability):
        self.value = probability

    def probability(self, features):
        return self.value


def evidence(**kwargs):
    return PairEvidence(person_a="a", person_b="b", name_a="Ivan Petrov", name_b="I. Petrov", **kwargs)


class CollaborationSafetyTest(unittest.TestCase):
    def test_small_team_diagnostics_do_not_change_fitted_feature_vector(self):
        raw = evidence(shared_coauthors=200, shared_publications=1)
        diagnostic = replace(raw, small_team_shared_coauthors=0, small_team_shared_publications=0)
        self.assertEqual(feature_vector(raw), feature_vector(diagnostic))

    def test_consortium_dependent_merge_is_deferred_not_separated(self):
        raw = evidence(shared_coauthors=200)
        self.assertEqual(resolve_pair(raw, model=CoauthorModel()).decision, Decision.MERGE)
        guarded = replace(raw, small_team_shared_coauthors=0, small_team_shared_publications=0)
        result = resolve_pair(guarded, model=CoauthorModel())
        self.assertEqual(result.decision, Decision.FIRST_MODEL)
        self.assertEqual(result.route, "large_team_support")
        self.assertEqual(result.probability, 0.98)

    def test_identifier_match_and_independent_small_team_support_survive(self):
        base = evidence(shared_coauthors=200, small_team_shared_coauthors=0, small_team_shared_publications=0)
        for pair in (
            replace(base, orcid_a="0000-1", orcid_b="0000-1"),
            replace(base, staff_id_a="staff", staff_id_b="staff"),
            replace(base, small_team_shared_coauthors=8),
        ):
            with self.subTest(pair=pair):
                self.assertEqual(resolve_pair(pair, model=CoauthorModel()).decision, Decision.MERGE)

    def test_guard_does_not_create_a_new_auto_separation(self):
        pair = evidence(shared_coauthors=200, small_team_shared_coauthors=0, small_team_shared_publications=0)
        self.assertEqual(resolve_pair(pair, model=ConstantModel(0.5)).decision, Decision.FIRST_MODEL)

    def test_matching_identifier_and_conflicting_initials_require_review(self):
        for identifiers in (
            {"orcid_a": "https://orcid.org/same", "orcid_b": "same"},
            {"staff_id_a": "staff", "staff_id_b": "staff"},
        ):
            with self.subTest(identifiers=identifiers):
                result = resolve_pair(evidence(initials_conflict=True, **identifiers))
                self.assertEqual(result.decision, Decision.REVIEW)
                self.assertEqual(result.route, "identity_name_conflict")

    def test_conflicting_identifiers_still_veto_conflicting_initials(self):
        result = resolve_pair(evidence(initials_conflict=True, orcid_a="one", orcid_b="two"))
        self.assertEqual(result.decision, Decision.SEPARATE)

    def test_optional_counts_are_validated(self):
        for changes in (
            {"small_team_shared_coauthors": -1},
            {"small_team_shared_coauthors": True},
            {"small_team_shared_coauthors": 1},
            {"small_team_shared_publications": 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evidence(**changes)

    def test_first_stage_receives_both_raw_and_small_team_counts(self):
        pair = evidence(shared_coauthors=246, small_team_shared_coauthors=0, small_team_shared_publications=0)
        payload = first_stage_payload(0, pair)["pairs"][0]
        self.assertEqual(payload["shared_coauthors"], 246)
        self.assertEqual(payload["small_team_shared_coauthors"], 0)

    def test_name_list_guard_has_narrow_syntax(self):
        for name in ("Aleksandra Vatian and Anatoly Shalyto", "Ivan Petrov & Anna Sidorova", "I. Petrov; A. Sidorov"):
            self.assertTrue(is_multi_person_name(name))
        for name in ("Anders Anderson", "Anna Maria de la Cruz", "John Smith", "Smith, John", "And Research"):
            self.assertFalse(is_multi_person_name(name))

    def test_malformed_record_is_reviewed_even_with_same_orcid(self):
        pair = replace(evidence(orcid_a="same", orcid_b="same"), name_a="Ivan Petrov and Anna Sidorova")
        resolution = resolve_pair(pair, model=ConstantModel(1))
        self.assertEqual(resolution.decision, Decision.REVIEW)
        self.assertEqual(resolution.route, "multiple_people_record")

    def test_malformed_pair_does_not_call_models_or_merge_in_either_planner(self):
        people = [
            Person(id="a", name_raw="Ivan Petrov and Anna Sidorova", is_member=True, orcid="same"),
            Person(id="b", name_raw="Anna Sidorova", is_member=True, orcid="same"),
        ]
        models = FakeModels(ModelVerdict(True, 1), ModelVerdict(True, 1))
        groups, report = plan_person_merges_resolved(people, {"a": "same", "b": "same"}, models=models)
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["route"], "multiple_people_record")
        self.assertEqual((models.first_calls, models.second_calls), ([], []))
        groups, report = plan_person_merges(people, {"a": "same", "b": "same"})
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["route"], "multiple_people_record")

    def test_stale_manual_same_cannot_merge_a_malformed_record(self):
        people = [
            Person(id="a", name_raw="Ivan Petrov and Anna Sidorova", is_member=True),
            Person(id="b", name_raw="Anna Sidorova", is_member=True),
        ]
        groups, report = plan_person_merges_resolved(people, {}, decisions={frozenset(("a", "b")): "same"})
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "disputed")

    def test_planner_excludes_joint_work_and_large_teams_from_small_team_count(self):
        people = [
            Person(
                id="a",
                name_raw="Ivan Petrov",
                is_member=True,
                authored=[{"publication_id": "w1"}, {"publication_id": "joint"}],
            ),
            Person(
                id="b",
                name_raw="I. Petrov",
                is_member=True,
                authored=[{"publication_id": "w2"}, {"publication_id": "joint"}],
            ),
        ]
        people.extend(
            Person(
                id=f"x{i}",
                name_raw=f"Other Family{i}",
                is_member=True,
                authored=[{"publication_id": "w1"}, {"publication_id": "w2"}],
            )
            for i in range(60)
        )
        people.append(Person(id="c", name_raw="Jane Smith", is_member=True, authored=[{"publication_id": "joint"}]))
        models = FakeModels(ModelVerdict(False, 0.8), ModelVerdict(False, 0.8))
        plan_person_merges_resolved(people, {}, models=models, logreg_model=ConstantModel(0.5))
        pair_id, pair = next((i, e) for i, e in models.first_calls if {e.person_a, e.person_b} == {"a", "b"})
        self.assertEqual(pair.shared_coauthors, 60)
        self.assertEqual(pair.small_team_shared_coauthors, 0)
        self.assertEqual(pair.small_team_shared_publications, 1)
        context = next(c for c in models.second_calls if c.pair_id == pair_id)
        self.assertEqual(context.as_payload()["collaboration_evidence"]["small_team_shared_coauthors"], 0)
        self.assertTrue(all(not c["small_team_supported"] for c in context.shared_coauthors))
