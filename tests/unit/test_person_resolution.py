import unittest

from pauk.graph.person_resolution import (
    MODEL_FEATURES,
    Decision,
    ModelVerdict,
    PairEvidence,
    ResearcherContext,
    ResolverPolicy,
    SecondStageContext,
    apply_first_verdict,
    apply_second_verdict,
    feature_vector,
    first_stage_payload,
    logistic_probability,
    parse_first_stage_response,
    parse_second_stage_response,
    resolve_cascade,
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
        self.assertEqual(len(features), 49)
        self.assertAlmostEqual(features["shared_coauthors"], 1.0986122886681098)
        self.assertEqual(features["surname_equal"], 1)
        self.assertEqual(features["compatible_name"], 1)
        self.assertEqual(features["long_token_compatible"], 1)
        self.assertEqual(features["remaining_long_token_conflict"], 0)

    def test_long_token_features_ignore_name_order(self):
        features = feature_vector(
            evidence(name_a="Valentin Malykh", name_b="Malykh, Valentin")
        )

        self.assertEqual(features["surname_equal"], 0)
        self.assertEqual(features["long_token_compatible"], 1)
        self.assertEqual(features["remaining_long_token_conflict"], 0)

    def test_long_token_features_tolerate_transliteration_spelling(self):
        features = feature_vector(
            evidence(name_a="E. A. Zernitskaya", name_b="Ekaterina Zernitckaia")
        )

        self.assertGreater(features["long_token_similarity_max"], 0.8)
        self.assertEqual(features["long_token_compatible"], 1)

    def test_remaining_full_names_expose_namesakes(self):
        features = feature_vector(evidence(name_a="Ivan Petrov", name_b="Igor Petrov"))

        self.assertEqual(features["long_token_compatible"], 1)
        self.assertEqual(features["remaining_long_token_conflict"], 1)

    def test_initials_cannot_fake_a_compatible_long_token(self):
        features = feature_vector(
            evidence(name_a="A. V. Shashkin", name_b="Alexander Vinogradov")
        )

        self.assertEqual(features["long_token_compatible"], 0)

    def test_probability_is_stable_for_a_known_feature_vector(self):
        self.assertAlmostEqual(logistic_probability(evidence()), 0.9951826138, places=9)

    def test_conflicting_identifier_is_a_hard_veto(self):
        result = resolve_pair(evidence(orcid_a="0000-0001", orcid_b="0000-0002"))

        self.assertEqual(result.decision, Decision.SEPARATE)
        self.assertEqual(result.route, "hard_veto")
        self.assertEqual(result.reason, "conflicting ORCID")

    def test_names_without_comparable_tokens_are_a_hard_veto(self):
        for name_a, name_b in (("李明", "李明"), ("李明", "王芳"), ("", "")):
            with self.subTest(name_a=name_a, name_b=name_b):
                features = feature_vector(evidence(name_a=name_a, name_b=name_b))
                self.assertEqual(features["exact_name"], 0)
                self.assertEqual(features["same_tokens"], 0)

                result = resolve_pair(evidence(name_a=name_a, name_b=name_b))
                self.assertEqual(result.decision, Decision.SEPARATE)
                self.assertEqual(result.route, "hard_veto")
                self.assertEqual(result.reason, "no comparable name")

    def test_matching_trusted_identifier_overrides_probability_zone(self):
        result = resolve_pair(
            evidence(
                name_a="X",
                name_b="Y",
                shared_coauthors=0,
                shared_departments=0,
                shared_fields=0,
                works_a=0,
                works_b=0,
                orcid_a="0000-0001",
                orcid_b="0000-0001",
            )
        )

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "same_orcid")

    def test_orcid_url_and_bare_value_are_the_same_identifier(self):
        result = resolve_pair(
            evidence(
                name_a="X",
                name_b="Y",
                orcid_a="https://orcid.org/0000-0001",
                orcid_b="0000-0001",
            )
        )

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "same_orcid")

    def test_policy_thresholds_are_configurable(self):
        result = resolve_pair(evidence(), ResolverPolicy(separate_below=0.3, merge_from=1.0))

        self.assertEqual(result.decision, Decision.FIRST_MODEL)

    def test_default_policy_separates_a_clearly_unrelated_pair(self):
        result = resolve_pair(
            evidence(
                name_a="John Smith",
                name_b="Maria Garcia",
                shared_coauthors=0,
                shared_departments=0,
                shared_fields=0,
                works_a=1,
                works_b=20,
                surname_occurrences_a=100,
                surname_occurrences_b=100,
            )
        )

        self.assertEqual(result.decision, Decision.SEPARATE)
        self.assertEqual(result.route, "logreg_low")

    def test_default_policy_merges_only_an_extreme_probability(self):
        result = resolve_pair(
            evidence(
                person_a="name_1",
                name_a="Alexander Petrov",
                name_b="Alexander Petrov",
                shared_coauthors=5,
                shared_publications=1,
                works_a=10,
                works_b=8,
                surname_occurrences_a=1,
                surname_occurrences_b=1,
            )
        )

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "logreg_high")

    def test_different_full_names_are_never_merged_on_initials_alone(self):
        result = resolve_pair(
            evidence(
                person_a="name_1",
                name_a="A. V. Shashkin",
                name_b="Alexander Vinogradov",
                shared_coauthors=0,
                shared_departments=0,
                shared_fields=0,
                shared_publications=0,
                works_a=3,
                works_b=1,
                surname_occurrences_a=1,
                surname_occurrences_b=1,
            ),
            ResolverPolicy(separate_below=0.0, merge_from=0.02),
        )

        self.assertEqual(result.decision, Decision.FIRST_MODEL)
        self.assertEqual(result.route, "surname_mismatch")

    def test_conflicting_remaining_full_names_require_model_review(self):
        result = resolve_pair(
            evidence(
                name_a="Andrey V. Lyamin",
                name_b="Andrey Volchek",
                shared_coauthors=0,
                shared_publications=0,
            ),
            ResolverPolicy(separate_below=0.0, merge_from=0.02),
        )

        self.assertEqual(result.decision, Decision.FIRST_MODEL)
        self.assertEqual(result.route, "surname_mismatch")

    def test_name_order_does_not_create_a_surname_mismatch(self):
        result = resolve_pair(
            evidence(name_a="Valentin Malykh", name_b="Malykh, Valentin"),
            ResolverPolicy(separate_below=0.0, merge_from=0.02),
        )

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "logreg_high")

    def test_a_transliterated_surname_is_not_a_mismatch(self):
        result = resolve_pair(
            evidence(
                name_a="Aleksey Grigorev",
                name_b="A.S. Grigoriev",
                shared_coauthors=0,
                shared_publications=0,
            ),
            ResolverPolicy(separate_below=0.0, merge_from=0.02),
        )

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "logreg_high")

    def test_a_joint_work_allows_a_merge_despite_a_name_conflict(self):
        result = resolve_pair(
            evidence(
                name_a="A. V. Shashkin",
                name_b="Alexander Vinogradov",
                shared_coauthors=0,
                shared_publications=2,
            ),
            ResolverPolicy(separate_below=0.0, merge_from=0.02),
        )

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "logreg_high")

    def test_uncertain_pair_requires_two_positive_model_verdicts(self):
        initial = resolve_pair(
            evidence(
                name_a="A. Petrov",
                name_b="Aleksandr Petrov",
                shared_coauthors=0,
                shared_departments=0,
                shared_fields=0,
                works_a=1,
                works_b=1,
                surname_occurrences_a=30,
                surname_occurrences_b=30,
            )
        )
        self.assertEqual(initial.decision, Decision.FIRST_MODEL)

        second = apply_first_verdict(initial, ModelVerdict(True, 0.91, "compatible name"))
        self.assertEqual(second.decision, Decision.SECOND_MODEL)

        rejected = apply_second_verdict(second, ModelVerdict(False, 0.87, "no independent support"))
        self.assertEqual(rejected.decision, Decision.SEPARATE)
        self.assertEqual(rejected.route, "qwen_second_separate")

    def test_cascade_reproduces_two_positive_verdict_requirement(self):
        pair = evidence(
            name_a="A. Petrov",
            name_b="Aleksandr Petrov",
            shared_coauthors=0,
            shared_departments=0,
            shared_fields=0,
            works_a=1,
            works_b=1,
            surname_occurrences_a=30,
            surname_occurrences_b=30,
        )

        result = resolve_cascade(
            pair,
            first_verdict=ModelVerdict(True, 0.91),
            second_verdict=ModelVerdict(True, 0.88),
        )

        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "qwen_second_merge")

    def test_first_stage_payload_matches_preview_contract(self):
        payload = first_stage_payload(
            42,
            evidence(
                person_a="name_fallback",
                orcid_a="https://orcid.org/0000-0001",
                orcid_b="0000-0001",
                shared_publications=1,
            ),
        )

        pair = payload["pairs"][0]
        self.assertEqual(pair["id"], 42)
        self.assertEqual(pair["orcid"], "same")
        self.assertTrue(pair["a"]["fallback_record"])
        self.assertEqual(pair["shared_publications"], 1)

    def test_model_response_contracts_are_strict(self):
        first = parse_first_stage_response(
            {"results": [{"id": 7, "duplicate": True, "confidence": 0.93, "reason": "match"}]},
            7,
        )
        second = parse_second_stage_response(
            {
                "id": 7,
                "same_person": False,
                "confidence": 0.82,
                "support": ["similar name"],
                "risk": ["no shared work"],
                "reason": "insufficient corroboration",
            },
            7,
        )

        self.assertTrue(first.duplicate)
        self.assertFalse(second.duplicate)
        self.assertEqual(second.support, ("similar name",))
        self.assertEqual(second.risk, ("no shared work",))

        with self.assertRaises(ValueError):
            parse_first_stage_response(
                {"results": [{"id": 8, "duplicate": True, "confidence": 0.93}]},
                7,
            )

    def test_second_stage_payload_keeps_impact_separate_from_identity_evidence(self):
        context = SecondStageContext(
            pair_id=9,
            researcher_a=ResearcherContext(
                person_id="A1",
                name="D. V. Denisov",
                aliases=("Dmitrii V. Denisov",),
                works=10,
            ),
            researcher_b=ResearcherContext(person_id="A2", name="Dmitrii V. Denisov", works=8),
            trusted_orcid_relation="one_missing",
            staff_identity_relation="both_missing",
            shared_work_ids=("W1", "W2"),
            shared_fields=("Photonics",),
            logreg_probability=0.87,
            first_verdict=ModelVerdict(True, 0.95, "compatible identity evidence"),
            impact_not_identity_evidence={"potential_cross_neighbor_pairs": 34},
        )

        payload = context.as_payload()
        self.assertEqual(payload["a"]["aliases"], ["Dmitrii V. Denisov"])
        self.assertTrue(payload["first_stage"]["same_person"])
        self.assertEqual(
            payload["impact_not_identity_evidence"]["potential_cross_neighbor_pairs"],
            34,
        )

    def test_negative_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            evidence(shared_coauthors=-1)

    def test_negative_first_verdict_finishes_without_second_call(self):
        initial = resolve_pair(
            evidence(
                name_a="A. Petrov",
                name_b="Aleksandr Petrov",
                shared_coauthors=0,
                shared_departments=0,
                shared_fields=0,
                works_a=1,
                works_b=1,
                surname_occurrences_a=30,
                surname_occurrences_b=30,
            )
        )

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
