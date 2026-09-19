import unittest

from pauk.pipeline.stages.author_names import required_name_field_issues
from scripts.plan_author_names_repair import (
    assign_repair_groups,
    valid_groups,
)


class RequiredNameFieldIssuesTest(unittest.TestCase):
    def test_complete_person_has_no_issues(self):
        person = {
            "surname_ru": "Никитин-Соколов",
            "first_name_ru": "Павел",
            "surname_en": "García-Márquez",
            "first_name_en": "D'Angelo",
        }
        self.assertEqual(required_name_field_issues(person), {})

    def test_missing_blank_and_non_string_fields_are_reported(self):
        person = {
            "surname_ru": None,
            "first_name_ru": "   ",
            "surname_en": 42,
        }
        self.assertEqual(
            list(required_name_field_issues(person)),
            ["surname_ru", "first_name_ru", "surname_en", "first_name_en"],
        )

    def test_wrong_alphabet_and_punctuation_only_fields_are_reported(self):
        person = {
            "surname_ru": "Zhukov",
            "first_name_ru": "---",
            "surname_en": "Жуков",
            "first_name_en": "Pavel",
        }

        issues = required_name_field_issues(person)

        self.assertEqual(set(issues), {"surname_ru", "first_name_ru", "surname_en"})
        self.assertIn("Cyrillic", issues["surname_ru"])
        self.assertIn("at least one letter", issues["first_name_ru"])
        self.assertIn("Latin", issues["surname_en"])


class RepairGroupAssignmentTest(unittest.TestCase):
    def test_invalid_groups_are_excluded(self):
        self.assertEqual(
            valid_groups({"groups": ["period-2025", "bad/group", None, "period-2025"]}),
            ["period-2025"],
        )

    def test_each_person_is_assigned_once_with_a_greedy_group_cover(self):
        people = [
            {"id": "A1", "groups": ["g1", "g2"]},
            {"id": "A2", "groups": ["g1"]},
            {"id": "A3", "groups": ["g2"]},
            {"id": "A4", "groups": []},
        ]

        assignments, unresolved = assign_repair_groups(people)

        assigned = [person_id for ids in assignments.values() for person_id in ids]
        self.assertEqual(sorted(assigned), ["A1", "A2", "A3"])
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(unresolved, ["A4"])
        self.assertEqual(assignments, {"g1": ["A1", "A2"], "g2": ["A3"]})


if __name__ == "__main__":
    unittest.main()
