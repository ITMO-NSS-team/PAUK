"""Unit test for graph_builder.py - an end-to-end check of
`GraphDataBuilder` on a small synthetic db in `pauk.cache`'s shape. Stage
logic (authorship indexing, department assignment, layout, node/edge
building) is tested separately in `test_authorship.py`/
`test_departments.py`/`test_layout.py`/`test_nodes.py` - this only checks
the shape (summary/detail split), not specific layout numbers.
"""

from __future__ import annotations

import unittest

from new_generate.graph_builder import GraphDataBuilder


class BuildGraphDataIntegrationTest(unittest.TestCase):
    @staticmethod
    def _sample_db():
        return {
            "persons": [
                {"id": "A1", "first_name_ru": "Иван", "second_name_ru": None, "surname_ru": "Иванов",
                 "first_name_en": "Ivan", "second_name_en": None, "surname_en": "Ivanov",
                 "name_ru": "Иванов Иван", "name_variants": [], "degree": "к.т.н.", "github": "ivanov", "orcid": None,
                 "openalex_id": "A123", "google_scholar": None, "openreview": None, "email": "ivanov@itmo.ru",
                 "affiliations": '[{"name": "ITMO"}]'},
            ],
            "publications": [
                {"id": "P1", "title": "Т" * 250, "journal": "Ж", "doi": "10.1/x",
                 "publication_date": "2024-01-01", "year": 2024, "has_code": True, "code_url": '["https://x"]',
                 "type": "article", "fields": ["Computer Science"], "funding": "[]", "versions": "[]",
                 "openalex_url": "https://openalex.org/W1", "abstract": "Абстракт"},
            ],
            "repositories": [
                # "owner" is deliberately absent here: pauk/cache/export.py
                # no longer returns it (see RepoNodeBuilder) - the fixture
                # should reflect the real snapshot shape, not the old one.
                {"id": "R1", "name": "repo", "url": "https://x", "description": "Описание", "stars_num": 5,
                 "has_readme": True, "license": "MIT", "contributors": ["ivanov"], "owner_type": "user"},
            ],
            "departments": [{"id": "d1", "name_ru": "Кафедра", "name_en": "Dept"}],
            "authorship": [{"pid": "P1", "per": "A1"}],
            "person_depts": [{"per": "A1", "did": "d1"}],
            "pub_depts": [{"pid": "P1", "did": "d1"}],
            "repo_pubs": [{"rid": "R1", "pid": "P1"}],
            "repo_persons": [{"rid": "R1", "per": "A1", "role": "maintainer"}],
            "repo_depts": [{"rid": "R1", "did": "d1"}],
        }

    def test_summary_has_no_personal_or_detail_fields(self):
        summary, _detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        author = summary["authors"][0]
        self.assertEqual(set(author), {"key", "kind", "dept", "label", "label_en", "pubs_count", "rank", "gx", "gy"})

    def test_detail_carries_the_fields_summary_does_not(self):
        _summary, detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        author_detail = detail["authors"][0]
        self.assertEqual(author_detail["key"], "A1")
        self.assertEqual(author_detail["degree"], "к.т.н.")
        self.assertEqual(author_detail["openalex_id"], "A123")
        self.assertEqual(author_detail["email"], "ivanov@itmo.ru")
        self.assertEqual(author_detail["affiliations"], [{"name": "ITMO"}])  # JSON-text parsed

    def test_summary_label_is_always_the_truncated_public_form(self):
        # graph-data.json is one shared file across build variants (see
        # graph_builder.py on public/private being decided by file location,
        # not content), so the map label is always truncated, not just for --public.
        summary, _detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        self.assertEqual(summary["authors"][0]["label"], "Ива.. И.")

    def test_author_detail_is_the_same_regardless_of_output_folder(self):
        # The full name is still available - via detail, not summary.
        _summary, detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        self.assertEqual(detail["authors"][0]["name_ru"], "Иванов Иван")

    def test_pub_detail_truncates_long_titles_and_parses_code_url(self):
        _summary, detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        pub_detail = detail["pubs"][0]
        self.assertEqual(len(pub_detail["label"]), 200)
        self.assertTrue(pub_detail["label"].endswith("…"))
        self.assertEqual(pub_detail["code_url"], ["https://x"])
        self.assertEqual(pub_detail["type"], "article")
        self.assertEqual(pub_detail["fields"], ["Computer Science"])
        self.assertEqual(pub_detail["funding"], [])  # "[]" -> [], not a string
        self.assertEqual(pub_detail["abstract"], "Абстракт")

    def test_repo_pub_and_author_edges_are_present(self):
        summary, _detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        self.assertEqual(summary["repo_pub_edges"], [{"s": "R1", "t": "P1"}])
        self.assertEqual(summary["repo_author_edges"], [{"s": "R1", "t": "A1", "role": "maintainer"}])

    def test_repo_detail_no_longer_has_an_owner_field(self):
        """Regression test for a bug from the OOP-restructure slice:
        RepoNodeBuilder used to read row["owner"], which export.py no longer
        returns - raised KeyError on the first real run."""
        _summary, detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        repo_detail = detail["repos"][0]
        self.assertEqual(
            set(repo_detail), {"key", "description", "url", "has_readme", "license", "contributors", "owner_type"}
        )
        self.assertEqual(repo_detail["contributors"], ["ivanov"])
