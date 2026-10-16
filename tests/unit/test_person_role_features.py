import math
import unittest
from dataclasses import replace
from unittest.mock import patch

from pauk.graph.person_resolution import Decision, PairEvidence, feature_vector, resolve_pair
from pauk.pipeline.person_role_features import alignment


class PersonRoleFeaturesTest(unittest.TestCase):
    @staticmethod
    def pair(first, second, **changes):
        return PairEvidence(person_a="a", person_b="b", name_a=first, name_b=second, **changes)

    def test_identical_names_use_primary_surname_regardless_of_alphabet(self):
        for name, surname in (
            ("Sergey Makarov", "makarov"),
            ("Andrey Bogdanov", "bogdanov"),
            ("Xiaolong Wang", "wang"),
            ("Wei Wang", "wang"),
            ("Alexey Kuznetsov", "kuznetsov"),
            ("Jean-Pierre Dupont", "dupont"),
        ):
            with self.subTest(name=name):
                match = alignment(name, name)
                self.assertEqual((match[1].surname, match[2].surname), (surname, surname))
                values = feature_vector(self.pair(name, name), {surname: 100})
                self.assertAlmostEqual(values["roles_v2__surname_rarity"], 1 / math.log2(202))

    def test_reversed_ambiguous_names_use_conservative_frequency(self):
        for first, second, surname, counts in (
            ("Sergey Makarov", "Makarov Sergey", "makarov", {"makarov": 100, "sergey": 1}),
            ("Xiaolong Wang", "Wang Xiaolong", "wang", {"wang": 100, "ksiaolong": 1}),
        ):
            for left, right in ((first, second), (second, first)):
                with self.subTest(left=left, right=right):
                    match = alignment(left, right, counts)
                    self.assertEqual((match[1].surname, match[2].surname), (surname, surname))
                    self.assertAlmostEqual(
                        feature_vector(self.pair(left, right), counts)["roles_v2__surname_rarity"],
                        1 / math.log2(202),
                    )

    def test_explicit_surname_anchors_reverse_order_even_if_less_frequent(self):
        for first, second in (
            ("Makarov, Sergey", "Sergey Makarov"),
            ("S. Makarov", "Makarov Sergey"),
            ("Wang, Xiaolong", "Wang Xiaolong"),
        ):
            surname = "wang" if "Wang" in first else "makarov"
            for left, right in ((first, second), (second, first)):
                with self.subTest(left=left, right=right):
                    match = alignment(left, right, {surname: 1, "sergey": 100, "ksiaolong": 100})
                    self.assertEqual((match[1].surname, match[2].surname), (surname, surname))

    def test_missing_frequencies_do_not_imply_rarity(self):
        for first, second, counts in (
            ("Sergey Makarov", "Sergey Makarov", {}),
            ("Sergey Makarov", "Makarov Sergey", {}),
            ("Ivan Makarov", "Ivan Makarev", {"makarov": 10}),
        ):
            with self.subTest(first=first, second=second):
                self.assertEqual(feature_vector(self.pair(first, second), counts)["roles_v2__surname_rarity"], 0)

    def test_pair_orientation_does_not_change_role_features(self):
        for first, second in (
            ("Sergey Makarov", "Makarov Sergey"),
            ("Grigoriev, Ivan", "Ivan Grigoryev"),
            ("Xiaolong Wang", "Wang, Xiaolong"),
        ):
            with self.subTest(first=first, second=second):
                counts = {"makarov": 12, "grigoriev": 10, "grigoryev": 3, "wang": 100}
                forward = feature_vector(self.pair(first, second), counts)
                backward = feature_vector(self.pair(second, first), counts)
                for key in ("surname_equal", "surname_rarity", "first_initial_equal", "given_full_conflicts", "roles_explicit"):
                    self.assertEqual(forward["roles_v2__" + key], backward["roles_v2__" + key])

    def test_unsupported_namesakes_never_skip_the_judge(self):
        for first, second in (
            ("Sergey Makarov", "Sergey Makarov"),
            ("Andrey Bogdanov", "Bogdanov Andrey"),
            ("Wei Wang", "Wang Wei"),
        ):
            with self.subTest(first=first, second=second), patch(
                "pauk.graph.person_resolution.logistic_probability", return_value=0.999
            ):
                item = self.pair(first, second, shared_departments=1, shared_fields=2)
                result = resolve_pair(item)
                self.assertEqual(result.decision, Decision.FIRST_MODEL)
                self.assertEqual(result.route, "namesake_without_collaboration")

    def test_supported_namesakes_and_identifiers_keep_their_routes(self):
        pair = self.pair("Sergey Makarov", "Sergey Makarov")
        for changes in (
            {"shared_coauthors": 1},
            {"shared_publications": 1, "shared_coauthors": 1},
            {"staff_id_a": "42", "staff_id_b": "42"},
            {"orcid_a": "0000-1", "orcid_b": "0000-1"},
        ):
            with self.subTest(changes=changes), patch(
                "pauk.graph.person_resolution.logistic_probability", return_value=0.999
            ):
                self.assertEqual(resolve_pair(replace(pair, **changes)).decision, Decision.MERGE)

    def test_identifier_conflict_still_separates_namesakes(self):
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.999):
            result = resolve_pair(self.pair("Sergey Makarov", "Sergey Makarov", orcid_a="0000-1", orcid_b="0000-2"))
        self.assertEqual(result.decision, Decision.SEPARATE)

    def test_shared_work_does_not_bypass_independent_evidence(self):
        for first, second in (("Huadong Wang", "Huadong Wang"), ("Ivan Petrov", "I. Petrov")):
            for shared_works in (1, 3):
                for coauthors, small_team in ((0, None), (0, 0), (50, 0)):
                    with self.subTest(names=(first, second), works=shared_works, coauthors=coauthors), patch(
                        "pauk.graph.person_resolution.logistic_probability", return_value=0.999
                    ):
                        result = resolve_pair(self.pair(
                            first, second, shared_publications=shared_works,
                            shared_coauthors=coauthors, small_team_shared_coauthors=small_team,
                            shared_fields=2, shared_departments=1,
                        ))
                        self.assertEqual(result.decision, Decision.FIRST_MODEL)
                        self.assertEqual(result.route, "joint_publication_without_independent_support")

    def test_shared_work_guard_preserves_identifiers_and_independent_support(self):
        pair = self.pair("Huadong Wang", "Huadong Wang", shared_publications=1, small_team_shared_coauthors=0)
        for changes in (
            {"orcid_a": "same", "orcid_b": "same"},
            {"staff_id_a": "42", "staff_id_b": "42"},
            {"shared_coauthors": 1, "small_team_shared_coauthors": 1},
        ):
            with self.subTest(changes=changes), patch(
                "pauk.graph.person_resolution.logistic_probability", return_value=0.999
            ):
                self.assertEqual(resolve_pair(replace(pair, **changes)).decision, Decision.MERGE)

    def test_shared_work_guard_does_not_override_low_score_or_identifier_conflict(self):
        pair = self.pair("Huadong Wang", "Huadong Wang", shared_publications=1)
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.01):
            self.assertEqual(resolve_pair(pair).decision, Decision.SEPARATE)
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.999):
            self.assertEqual(resolve_pair(replace(pair, orcid_a="one", orcid_b="two")).decision, Decision.SEPARATE)
