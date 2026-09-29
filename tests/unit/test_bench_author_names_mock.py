import unittest

from pauk.pipeline.stages.author_names import required_name_field_issues
from tests.bench.mocks import MockOpenRouterClient
from tests.bench.universe import RUSSIAN_NAMES_CATALOG


class MockOpenRouterClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MockOpenRouterClient(RUSSIAN_NAMES_CATALOG)

    def reply_for(self, name: str) -> dict:
        reply = self.client.chat_json(f"Name in mixed:\n  {name}")
        self.assertIsNotNone(reply)
        return reply

    def test_catalog_match_has_both_alphabets(self):
        reply = self.reply_for("Oleg Ivanov")

        self.assertEqual(reply["matched_candidate"], 0)
        self.assertEqual(reply["surname_ru"], "Иванов")
        self.assertEqual(reply["first_name_ru"], "Олег")
        self.assertEqual(reply["second_name_ru"], "Петрович")
        self.assertEqual(reply["surname_en"], "Ivanov")
        self.assertEqual(reply["first_name_en"], "Oleg")
        self.assertEqual(reply["second_name_en"], "Petrovich")
        self.assertEqual(required_name_field_issues(reply), {})

    def test_fallback_replies_satisfy_required_field_contract(self):
        cases = {
            "José Álvarez-Müller": ("José", "Álvarez-Müller"),
            "Екатерина Смирнова": ("Ekaterina", "Smirnova"),
            "Filler Person 0": ("Filler", "Person 0"),
            "D. A. Kovalev": ("D. A.", "Kovalev"),
            "Jan van der Berg": ("Jan", "van der Berg"),
        }

        for name, expected_english_parts in cases.items():
            with self.subTest(name=name):
                reply = self.reply_for(name)
                self.assertEqual(required_name_field_issues(reply), {})
                self.assertEqual(
                    (reply["first_name_en"], reply["surname_en"]),
                    expected_english_parts,
                )
                self.assertIs(self.client.last_response, reply)


if __name__ == "__main__":
    unittest.main()
