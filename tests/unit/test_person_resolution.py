import unittest

from pauk.graph.person_resolution import (
    MODEL_FEATURES,
    Decision,
    ModelVerdict,
    PairEvidence,
    ResolverPolicy,
    apply_first_verdict,
    apply_second_verdict,
    feature_vector,
    logistic_probability,
    resolve_pair,
)


def evidence(**changes):
    values = {
        "person_a": "A1",
        "name_a": "Alexander Petrov",
        "person_b": "A2",
        "name_b": "Alexander Petrov",
        "shared_coauthors": 2,
        "shared_departments": 1,
        "shared_fields": 2,
        "works_a": 8,
        "works_b": 3,
        "surname_occurrences_a": 5,
        "surname_occurrences_b": 5,
    }
    values.update(changes)
    return PairEvidence(**values)


class PersonResolutionTest(unittest.TestCase):
    def test_feature_vector_matches_the_fitted_model_contract(self):
        features = feature_vector(evidence())

        self.assertEqual(tuple(features), MODEL_FEATURES)
        self.assertEqual(len(features), 46)
        self.assertAlmostEqual(features["shared_coauthors"], 1.0986122886681098)
        self.assertEqual(features["surname_equal"], 1)
        self.assertEqual(features["compatible_name"], 1)

    def test_probability_is_stable_for_a_known_feature_vector(self):
        self.assertAlmostEqual(logistic_probability(evidence()), 0.9006393934, places=9)

    def test_conflicting_identifier_is_a_hard_veto(self):
        result = resolve_pair(evidence(orcid_a="0000-0001", orcid_b="0000-0002"))

        self.assertEqual(result.decision, Decision.SEPARATE)
        self.assertEqual(result.route, "hard_veto")
        self.assertEqual(result.reason, "conflicting ORCID")

    def test_matching_trusted_identifier_overrides_probability_zone(self):
        result = resolve_pair(evidence(
            name_a="X",
            name_b="Y",
            shared_coauthors=0,
            shared_departments=0,
            shared_fields=0,
            works_a=0,
            works_b=0,
            orcid_a="0000-0001",
            orcid_b="0000-0001",
        ))

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "same_orcid")

    def test_policy_thresholds_are_configurable(self):
        result = resolve_pair(evidence(), ResolverPolicy(separate_below=0.3, merge_from=1.0))

        self.assertEqual(result.decision, Decision.FIRST_MODEL)

    def test_default_policy_separates_a_clearly_unrelated_pair(self):
        result = resolve_pair(evidence(
            name_a="John Smith",
            name_b="Maria Garcia",
            shared_coauthors=0,
            shared_departments=0,
            shared_fields=0,
            works_a=1,
            works_b=20,
            surname_occurrences_a=100,
            surname_occurrences_b=100,
        ))

        self.assertEqual(result.decision, Decision.SEPARATE)
        self.assertEqual(result.route, "logreg_low")

    def test_default_policy_merges_only_an_extreme_probability(self):
        result = resolve_pair(evidence(
            person_a="name_1",
            name_a="Alexander Petrov",
            name_b="Alexander Petrov",
            shared_coauthors=5,
            shared_publications=1,
            works_a=10,
            works_b=8,
            surname_occurrences_a=1,
            surname_occurrences_b=1,
        ))

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "logreg_high")

    def test_uncertain_pair_requires_two_positive_model_verdicts(self):
        initial = resolve_pair(evidence(
            name_a="A. Petrov",
            name_b="Aleksandr Petrov",
            shared_coauthors=0,
            shared_departments=0,
            shared_fields=0,
            works_a=1,
            works_b=1,
            surname_occurrences_a=30,
            surname_occurrences_b=30,
        ))
        self.assertEqual(initial.decision, Decision.FIRST_MODEL)

        second = apply_first_verdict(initial, ModelVerdict(True, 0.91, "compatible name"))
        self.assertEqual(second.decision, Decision.SECOND_MODEL)

        rejected = apply_second_verdict(second, ModelVerdict(False, 0.87, "no independent support"))
        self.assertEqual(rejected.decision, Decision.SEPARATE)
        self.assertEqual(rejected.route, "qwen_second_separate")

    def test_negative_first_verdict_finishes_without_second_call(self):
        initial = resolve_pair(evidence(
            name_a="A. Petrov",
            name_b="Aleksandr Petrov",
            shared_coauthors=0,
            shared_departments=0,
            shared_fields=0,
            works_a=1,
            works_b=1,
            surname_occurrences_a=30,
            surname_occurrences_b=30,
        ))

        result = apply_first_verdict(initial, ModelVerdict(False, 0.78, "insufficient evidence"))

        self.assertEqual(result.decision, Decision.SEPARATE)
        self.assertEqual(result.route, "qwen_first_separate")

    def test_model_confidence_outside_contract_is_rejected(self):
        with self.assertRaises(ValueError):
            ModelVerdict(True, 0.49)

        with self.assertRaises(ValueError):
            ModelVerdict("yes", 0.9)


if __name__ == "__main__":
    unittest.main()
