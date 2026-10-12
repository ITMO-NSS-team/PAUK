import unittest
from dataclasses import replace
from unittest.mock import patch

import mongomock

from pauk.graph.person_resolution import (
    ModelVerdict,
    PairEvidence,
    ResearcherContext,
    SecondStageContext,
    feature_vector,
    first_stage_payload,
    logistic_probability,
    shared_identity_facts,
)
from pauk.models import Person
from pauk.pipeline.person_name_facts import name_comparison_facts
from pauk.pipeline.person_resolution import OpenRouterResolutionModels
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.settings import Settings


def facts(first, second):
    return name_comparison_facts(
        Person(id="A1", name_raw=first, is_itmo=True), Person(id="A2", name_raw=second, is_itmo=True)
    )


class PersonIdentityFactsTest(unittest.TestCase):
    def test_missing_middle_component_is_not_a_conflict(self):
        for first, second in (
            ("Dmitry Fedorov", "Fedorov, Dmitry Alekseevich"),
            ("A. Kozyreva", "А.И. Козырева"),
            ("Lavrentii Danilov", "Лаврентий Глебович Данилов"),
        ):
            with self.subTest(first=first):
                row = facts(first, second)["alignments"][0]
                relations = [c["relation"] for c in row["given_components"]]
                self.assertIn("missing_on_one_side", relations)
                self.assertNotIn("different_initials", relations)
                self.assertNotIn("different_full_tokens", relations)

    def test_transliterated_initial_can_expand_to_full_patronymic(self):
        row = facts("А. Yu. Babenko", "Alina Yur'evna Babenko")["alignments"][0]
        self.assertEqual([c["relation"] for c in row["given_components"]], ["compatible_initial"] * 2)

    def test_incompatible_full_given_names_remain_visible(self):
        row = facts("Ivan Petrov", "Igor Petrov")["alignments"][0]
        self.assertEqual(row["given_components"][0]["relation"], "different_full_tokens")

    def test_incompatible_middle_initial_remains_visible(self):
        row = facts("A. A. Petrov", "A. V. Petrov")["alignments"][0]
        self.assertEqual(row["given_components"][1]["relation"], "different_initials")

    def test_explicit_reverse_order_matches_forward_order(self):
        row = facts("Valentin Malykh", "Malykh, Valentin")["alignments"][0]
        self.assertEqual(row["surname_relation"], "equal_after_normalization")
        self.assertEqual(row["given_components"][0]["relation"], "equal_after_normalization")

    def test_ambiguous_or_missing_roles_do_not_invent_a_surname(self):
        for name in ("", "A. A."):
            with self.subTest(name=name):
                result = facts(name, "Anna Ivanova")
                self.assertEqual(result["alignments"], [])
                self.assertEqual(result["meaning"], "spelling_observations_not_identity_proof")

    def test_an_alias_cannot_hide_a_primary_name_conflict(self):
        first = Person(id="A1", name_raw="Ivan Petrov", name_variants=["Igor Petrov"], is_itmo=True)
        second = Person(id="A2", name_raw="Igor Petrov", is_itmo=True)
        result = name_comparison_facts(first, second)
        self.assertEqual(result["alignments"][0]["given_components"][0]["relation"], "different_full_tokens")

    def test_facts_do_not_change_fitted_features_or_probability(self):
        pair = PairEvidence("A1", "Nikolay Nikitin", "A2", "Nikolay O. Nikitin")
        enriched = replace(pair, name_facts=facts(pair.name_a, pair.name_b))
        self.assertEqual(feature_vector(pair), feature_vector(enriched))
        self.assertEqual(logistic_probability(pair), logistic_probability(enriched))

    def test_identifier_relations_are_normalized_in_shared_facts(self):
        pair = PairEvidence("A1", "Name", "A2", "Name", orcid_a="https://orcid.org/0000-0001", orcid_b="0000-0001")
        self.assertEqual(shared_identity_facts(pair)["orcid_relation"], "same")

    def test_pipeline_sends_identical_facts_to_both_views(self):
        people = [
            Person(id="A1", name_raw="Nikolay Nikitin", is_itmo=True),
            Person(id="A2", name_raw="Nikolay O. Nikitin", is_itmo=True),
        ]
        test = self

        class Capture:
            def first_many(self, items):
                self.first = {
                    i: first_stage_payload(i, evidence)["pairs"][0]["identity_facts"] for i, evidence in items
                }
                return {i: ModelVerdict(False, 0.8) for i, _ in items}

            def second_many(self, contexts):
                test.assertEqual(len(contexts), 1)
                for context in contexts:
                    test.assertEqual(context.as_judge_payload()["identity_facts"], self.first[context.pair_id])
                return {context.pair_id: ModelVerdict(False, 0.8) for context in contexts}

        self.assertEqual(plan_person_merges_resolved(people, {}, models=Capture()), ([], []))

    def test_blind_payload_and_cache_ignore_previous_verdict_score_and_impact(self):
        context = SecondStageContext(
            pair_id=7,
            researcher_a=ResearcherContext("A1", "Anna Ivanova"),
            researcher_b=ResearcherContext("A2", "A. Ivanova"),
            trusted_orcid_relation="both_missing",
            staff_identity_relation="both_missing",
            identity_facts={"schema_version": 1},
        )
        changed = replace(
            context,
            first_verdict=ModelVerdict(False, 0.99, "different"),
            logreg_probability=0.99,
            impact_not_identity_evidence={"potential_cross_neighbor_pairs": 999},
        )
        self.assertNotEqual(context.as_payload(), changed.as_payload())
        self.assertEqual(context.as_judge_payload(), changed.as_judge_payload())
        for field in ("first_stage", "logreg_probability", "impact_not_identity_evidence"):
            self.assertNotIn(field, context.as_judge_payload())
        models = OpenRouterResolutionModels(Settings(), mongomock.MongoClient()["test"], "group")
        with patch.object(models, "_run", return_value={}) as run:
            models.second_many([context, changed])
        left, right = run.call_args.args[0]
        self.assertEqual(left.payload, context.as_judge_payload())
        self.assertEqual(models._fingerprint(left), models._fingerprint(right))

    def test_cache_changes_when_factual_evidence_changes(self):
        base = SecondStageContext(
            0, ResearcherContext("A1", "Name"), ResearcherContext("A2", "Name"), "both_missing", "both_missing"
        )
        models = OpenRouterResolutionModels(Settings(), mongomock.MongoClient()["test"], "group")
        with patch.object(models, "_run", return_value={}) as run:
            models.second_many([base, replace(base, identity_facts={"schema_version": 1})])
        left, right = run.call_args.args[0]
        self.assertNotEqual(models._fingerprint(left), models._fingerprint(right))
