import copy
import unittest

from pauk.graph.client import _merge_duplicate_properties


class RepositoryMergeNamesTest(unittest.TestCase):
    def test_missing_or_blank_name_uses_known_duplicate_name(self):
        for canonical in ({}, {"name": None}, {"name": ""}, {"name": " \t\n\u00a0"}):
            with self.subTest(canonical=canonical):
                duplicate = {"id": "duplicate", "name": "Known-Repository"}
                self.assertEqual(
                    _merge_duplicate_properties("Repository", canonical, duplicate),
                    {"name": "Known-Repository"},
                )

    def test_filled_name_is_not_replaced(self):
        for duplicate_name in ("Different-Repository", "", " \t", None, 0):
            with self.subTest(duplicate_name=duplicate_name):
                self.assertEqual(
                    _merge_duplicate_properties(
                        "Repository", {"name": "Canonical"}, {"name": duplicate_name}
                    ),
                    {},
                )

    def test_empty_or_invalid_duplicate_name_does_not_fill_a_gap(self):
        for canonical in ({}, {"name": None}, {"name": ""}, {"name": "  "}):
            for duplicate in ({}, {"name": None}, {"name": ""}, {"name": " \t\n"}, {"name": 0}):
                with self.subTest(canonical=canonical, duplicate=duplicate):
                    self.assertEqual(_merge_duplicate_properties("Repository", canonical, duplicate), {})

    def test_filling_name_preserves_original_value_and_inputs(self):
        canonical = {"id": "canonical", "name": "", "stars_num": 0}
        duplicate = {"id": "duplicate", "name": " Known-Repository ", "stars_num": 42}
        before = copy.deepcopy((canonical, duplicate))
        updates = _merge_duplicate_properties("Repository", canonical, duplicate)
        self.assertEqual(updates, {"name": " Known-Repository "})
        self.assertEqual((canonical, duplicate), before)
        self.assertEqual(_merge_duplicate_properties("Repository", {**canonical, **updates}, duplicate), {})

    def test_other_scalar_fields_keep_existing_semantics(self):
        self.assertEqual(
            _merge_duplicate_properties("Repository", {"description": ""}, {"description": "Details"}),
            {},
        )
        self.assertEqual(
            _merge_duplicate_properties("Repository", {"description": None}, {"description": ""}),
            {"description": ""},
        )
        self.assertEqual(_merge_duplicate_properties("Organization", {"name": ""}, {"name": "Known"}), {})

    def test_boolean_and_list_preservation_stays_unchanged(self):
        self.assertEqual(
            _merge_duplicate_properties(
                "Repository",
                {"name": "", "has_readme": False, "contributors": ["alice"]},
                {"name": "Known", "has_readme": True, "contributors": ["alice", "bob"]},
            ),
            {"name": "Known", "has_readme": True, "contributors": ["alice", "bob"]},
        )


if __name__ == "__main__":
    unittest.main()
