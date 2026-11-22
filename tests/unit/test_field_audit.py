import copy
import unittest

from pauk.graph.field_audit import audit_snapshot


class FieldAuditTest(unittest.TestCase):
    def test_null_blank_and_whitespace_names_are_reported_without_mutation(self):
        snapshot = {"repositories": [
            {"id": f"R{i}", "name": value, "url": "https://github.com/org/repo"}
            for i, value in enumerate((None, "", " \t\n"))
        ]}
        before = copy.deepcopy(snapshot)
        report = audit_snapshot(snapshot)
        self.assertEqual(len(report["findings"]), 3)
        self.assertTrue(all(row["status"] == "empty" for row in report["findings"]))
        self.assertEqual(snapshot, before)
        self.assertEqual(report["writes"], 0)

    def test_unexported_field_is_not_claimed_to_be_empty_in_graph(self):
        report = audit_snapshot({"persons": [{"id": "P1"}]})
        self.assertEqual(report["findings"][0]["status"], "not_exported")
        self.assertEqual(report["coverage"]["repositories"]["status"], "not_exported")

    def test_optional_fields_and_missing_translation_are_not_errors(self):
        report = audit_snapshot({
            "repositories": [{"id": "R1", "name": "repo", "url": "https://github.com/org/repo",
                              "stars_num": 0, "description": None}],
            "persons": [{"id": "P1", "name_raw": "Иван Петров", "name_en": None, "email": None}],
            "departments": [{"id": "D1", "name_ru": "Факультет", "name_en": ""}],
        })
        self.assertEqual(report["findings"], [])

    def test_invalid_types_are_distinct_from_empty_strings(self):
        report = audit_snapshot({"publications": [{"id": "W1", "title": 0}]})
        self.assertEqual(report["findings"][0]["status"], "invalid_type")

    def test_full_property_dump_identifies_absent_fields(self):
        report = audit_snapshot({"repositories": [{"id": "R1", "url": "https://github.com/org/repo"}]},
                                complete_properties=True)
        self.assertEqual(report["findings"][0]["status"], "missing")

    def test_partial_name_projection_does_not_prove_all_names_are_empty(self):
        snapshot = {"persons": [{"id": "P1", "name_ru": None, "name_en": None}]}
        self.assertEqual(audit_snapshot(snapshot)["findings"][0]["status"], "incomplete_projection")
        self.assertEqual(audit_snapshot(snapshot, complete_properties=True)["findings"][0]["status"], "empty")

    def test_malformed_table_is_rejected(self):
        with self.assertRaises(ValueError):
            audit_snapshot({"repositories": {"R1": {}}})

    def test_default_title_is_reported_separately(self):
        report = audit_snapshot({"publications": [{"id": "W1", "title": "Untitled"}]})
        self.assertEqual(report["findings"][0]["status"], "placeholder")

    def test_raw_name_does_not_prove_gui_has_a_label(self):
        row = {"id": "P1", "name_raw": "Sergey Ivanov", "name_ru": None,
               "name_en": "", "surname_ru": None, "surname_en": None}
        report = audit_snapshot({"persons": [row]})
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["display_findings"][0]["status"], "missing_display_name")
        self.assertTrue(report["display_findings"][0]["raw_name_available"])

    def test_unknown_projection_does_not_imply_missing_gui_label(self):
        report = audit_snapshot({"persons": [{"id": "P1", "name_raw": "Sergey Ivanov"}]})
        self.assertEqual(report["display_findings"], [])

    def test_available_translation_is_sufficient_for_gui_fallback(self):
        report = audit_snapshot({"persons": [{"id": "P1", "name_en": "Sergey Ivanov"}]},
                                complete_properties=True)
        self.assertEqual(report["display_findings"], [])


if __name__ == "__main__":
    unittest.main()
