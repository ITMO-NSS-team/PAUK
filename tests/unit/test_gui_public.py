"""The public variant of the site data: nothing personal or internal reaches it.

Checks every file `write_site_data` puts into `public/` as published, by its
words and its JSON keys, not the fields the site happens to show.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from pauk.cache.graph_snapshot import write_snapshot
from pauk.gui.graph_builder.builder import write_site_data
from pauk.gui.graph_builder.public import mask_free_text_name, public_author_labels
from pauk.gui.graph_builder.tests.test_graph_builder import BuildGraphDataIntegrationTest

PERSONAL_FIELDS = {
    "email", "orcid", "google_scholar", "github", "degree", "affiliations", "openalex_id",
    "name_ru", "name_en", "name_variants", "other_names",
    "first_name_ru", "second_name_ru", "surname_ru", "first_name_en", "second_name_en", "surname_en",
}
SERVICE_FIELDS = {"created_at", "updated_at", "versions"}
# Every given name and patronymic in the fixture, in both alphabets.
GIVEN_NAMES = {"Иван", "Ivan", "Пётр", "Петрович", "Petr", "Petrovich", "Stepan", "Evgenievich", "Степан", "Евгеньевич"}
PERSONAL_VALUES = {
    "petrov@itmo.ru", "0000-0002-1825-0097", "petrov-gh", "scholar123", "к.ф.-м.н.",
    "Institute of Secret Studies", "Petrov Petr Petrovich", "2026-08-14T10:23:45.123Z",
}


def _sample_db() -> dict:
    db = BuildGraphDataIntegrationTest._sample_db()
    petrov = {
        "id": "A5000000002", "first_name_ru": "Пётр", "second_name_ru": "Петрович", "surname_ru": "Петров",
        "first_name_en": "Petr", "second_name_en": "Petrovich", "surname_en": "Petrov",
        "name_ru": "Петров Пётр Петрович", "name_en": "Petrov Petr Petrovich",
        "name_variants": ["Petrov Petr Petrovich", "P. P. Petrov"], "other_names": ["Petr Petrov"],
        "degree": "к.ф.-м.н.", "github": "petrov-gh", "orcid": "0000-0002-1825-0097",
        "openalex_id": "A5000000002", "google_scholar": "scholar123", "email": "petrov@itmo.ru",
        "affiliations": '[{"name": "Institute of Secret Studies", "ror": "", "years": [2020], "source": "orcid"}]',
        "created_at": "2026-08-14T10:23:45.123Z", "updated_at": "2026-09-01T08:00:00.5Z",
    }
    # A manual record: no name parts, and the key itself spells the name out.
    manual = {
        "id": "manual_person_kochemazov_stepan", "first_name_ru": None, "second_name_ru": None, "surname_ru": None,
        "first_name_en": None, "second_name_en": None, "surname_en": None,
        "name_ru": "Кочемазов Степан Евгеньевич", "name_en": "Kochemazov Stepan Evgenievich",
        "name_variants": [], "degree": None, "github": None, "orcid": None, "openalex_id": None,
        "google_scholar": None, "email": None, "affiliations": None, "created_at": None, "updated_at": None,
    }
    db["persons"] += [petrov, manual]
    db["authorship"] += [
        {"pid": "P1", "per": "A5000000002", "position": 1, "is_corresponding": True},
        {"pid": "P1", "per": "manual_person_kochemazov_stepan", "position": 3, "is_corresponding": False},
    ]
    db["person_depts"] += [{"per": "A5000000002", "did": "d1"}, {"per": "manual_person_kochemazov_stepan", "did": "d1"}]
    db["repo_persons"].append({"rid": "R1", "per": "A5000000002", "role": "owner"})
    return db


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


class PublicBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        snapshot = Path(tmp.name) / "snapshot.json"
        write_snapshot(snapshot, _sample_db())
        write_site_data(snapshot, Path(tmp.name) / "gui", seed=1)
        public = Path(tmp.name) / "gui" / "public"
        cls.texts = {path.name: path.read_text(encoding="utf-8") for path in public.iterdir()}
        cls.files = {name: json.loads(text) for name, text in cls.texts.items()}
        cls.graph = cls.files["graph-data.json"]

    def test_no_given_name_or_patronymic_appears_as_a_word_in_any_file(self):
        for name, text in self.texts.items():
            words = set(re.findall(r"\w+", text))
            self.assertEqual(words & GIVEN_NAMES, set(), name)

    def test_no_personal_value_or_original_author_key_appears_in_any_file(self):
        original_keys = {'"A1"', '"A5000000002"', "manual_person_kochemazov_stepan"}
        for name, text in self.texts.items():
            for value in PERSONAL_VALUES | original_keys:
                self.assertNotIn(value, text, name)

    def test_no_personal_or_service_field_appears_as_a_key_in_any_file(self):
        for name, data in self.files.items():
            if name == "graph-data.json":
                # Department and group tables legitimately carry name_en/name_variants.
                data = {k: v for k, v in data.items() if k not in ("departments", "repo_groups")}
            self.assertEqual(_keys(data) & (PERSONAL_FIELDS | SERVICE_FIELDS), set(), name)

    def test_labels_are_surname_and_initials(self):
        labels = {(a["label"], a["label_en"]) for a in self.graph["authors"]}
        self.assertEqual(
            labels,
            {("Иванов И.", "Ivanov I."), ("Петров П. П.", "Petrov P. P."), ("Кочемазов С. Е.", "Kochemazov S. E.")},
        )

    def test_renumbered_keys_stay_consistent_across_edges_and_detail(self):
        authors = {a["key"] for a in self.graph["authors"]}
        self.assertEqual(authors, {"a0", "a1", "a2"})
        self.assertLessEqual({e["s"] for e in self.graph["all_edges"]}, authors)
        self.assertLessEqual({e["s"] for e in self.graph["coauth_edges"]} | {e["t"] for e in self.graph["coauth_edges"]}, authors)
        self.assertLessEqual({e["t"] for e in self.graph["repo_author_edges"]}, authors)
        details = {d["key"]: d for d in self.files["authors-detail.json"]}
        self.assertEqual(set(details), authors)
        self.assertEqual(set(next(iter(details.values()))), {"key", "pub_roles"})
        self.assertIn({"P1": {"position": 1, "corresponding": True}}, [d["pub_roles"] for d in details.values()])

    def test_publication_and_repository_cards_keep_what_the_site_shows(self):
        pub = self.files["pubs-detail.json"][0]
        self.assertEqual(pub["abstract"], "Абстракт")
        self.assertEqual(self.files["repos-detail.json"][0]["url"], "https://x")


class MaskingTest(unittest.TestCase):
    def test_free_text_keeps_the_first_word_and_initials_the_rest(self):
        self.assertEqual(mask_free_text_name("Иванов Иван Иванович"), "Иванов И. И.")
        self.assertEqual(mask_free_text_name("Ivanov Ivan"), "Ivanov I.")
        self.assertEqual(mask_free_text_name("Gusarov D.A."), "Gusarov D. A.")
        self.assertEqual(mask_free_text_name("Иванов"), "Иванов")
        self.assertEqual(mask_free_text_name(None), "")

    def test_english_label_falls_back_to_the_masked_russian_one(self):
        row = {"surname_ru": "Иванов", "first_name_ru": "Иван", "second_name_ru": "Иванович"}
        self.assertEqual(public_author_labels(row), ("Иванов И. И.", "Иванов И. И."))


if __name__ == "__main__":
    unittest.main()
