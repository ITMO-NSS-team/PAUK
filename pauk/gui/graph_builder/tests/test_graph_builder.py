"""Unit test for builder.py - an end-to-end check of
`GraphDataBuilder` on a small synthetic db in `pauk.cache`'s shape. Stage
logic (authorship indexing, department assignment, layout, node/edge
building) is tested separately in `test_authorship.py`/
`test_departments.py`/`test_layout.py`/`test_nodes.py` - this only checks
the shape (summary/detail split), not specific layout numbers.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pauk.cache.graph_snapshot import write_snapshot
from pauk.gui.graph_builder.builder import GraphDataBuilder, write_site_data


class BuildGraphDataIntegrationTest(unittest.TestCase):
    @staticmethod
    def _sample_db():
        return {
            "persons": [
                {"id": "A1", "first_name_ru": "Иван", "second_name_ru": None, "surname_ru": "Иванов",
                 "first_name_en": "Ivan", "second_name_en": None, "surname_en": "Ivanov",
                 "name_ru": "Иванов Иван", "name_variants": [], "degree": "к.т.н.", "github": "ivanov", "orcid": None,
                 "openalex_id": "A123", "google_scholar": None, "openreview": None, "email": "ivanov@itmo.ru",
                 "affiliations": '[{"name": "ITMO"}]',
                 "created_at": "2026-08-14T10:23:45.123Z", "updated_at": "2026-09-01T08:00:00.5Z"},
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
            "authorship": [{"pid": "P1", "per": "A1", "position": 2, "is_corresponding": True}],
            "person_depts": [{"per": "A1", "did": "d1"}],
            "pub_depts": [{"pid": "P1", "did": "d1"}],
            "repo_pubs": [{"rid": "R1", "pid": "P1"}],
            "repo_persons": [{"rid": "R1", "per": "A1", "role": "maintainer"}],
            "repo_depts": [{"rid": "R1", "did": "d1"}],
        }

    def test_summary_has_no_personal_or_detail_fields(self):
        summary, _detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        author = summary["authors"][0]
        self.assertEqual(set(author), {"key", "kind", "is_itmo", "dept", "label", "label_en", "pubs_count", "rank", "gx", "gy"})

    def test_detail_carries_the_fields_summary_does_not(self):
        _summary, detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        author_detail = detail["authors"][0]
        self.assertEqual(author_detail["key"], "A1")
        self.assertEqual(author_detail["degree"], "к.т.н.")
        self.assertEqual(author_detail["openalex_id"], "A123")
        self.assertEqual(author_detail["email"], "ivanov@itmo.ru")
        self.assertEqual(author_detail["affiliations"], [{"name": "ITMO"}])  # JSON-text parsed
        self.assertEqual(author_detail["created_at"], "2026-08-14T10:23:45.123Z")
        self.assertEqual(author_detail["updated_at"], "2026-09-01T08:00:00.5Z")

    def test_author_detail_carries_position_and_corresponding_per_publication(self):
        db = self._sample_db()
        db["publications"].append({**db["publications"][0], "id": "P2"})
        db["authorship"].append({"pid": "P2", "per": "A1", "position": 1, "is_corresponding": None})
        _summary, detail = GraphDataBuilder(db, seed=1).build()
        self.assertEqual(
            detail["authors"][0]["pub_roles"],
            {"P1": {"position": 2, "corresponding": True}, "P2": {"position": 1, "corresponding": False}},
        )

    def test_external_coauthor_of_an_itmo_publication_becomes_a_regular_author_node(self):
        db = self._sample_db()
        db["persons"][0]["is_itmo"] = True
        external = {**db["persons"][0], "id": "E1", "is_itmo": False, "surname_ru": "Смит", "email": None}
        stranger = {**external, "id": "E2"}
        db["persons"] += [external, stranger]
        db["publications"].append({**db["publications"][0], "id": "P2"})
        db["authorship"] += [
            {"pid": "P1", "per": "E1", "position": 1, "is_corresponding": False},
            # E1 and E2 alone on P2 - no ITMO author, so neither P2 nor E2 is on the map.
            {"pid": "P2", "per": "E1", "position": 1, "is_corresponding": False},
            {"pid": "P2", "per": "E2", "position": 2, "is_corresponding": False},
        ]
        summary, detail = GraphDataBuilder(db, seed=1).build()

        authors = {a["key"]: a for a in summary["authors"]}
        self.assertEqual(set(authors), {"A1", "E1"})
        self.assertFalse(authors["E1"]["is_itmo"])
        self.assertTrue(authors["A1"]["is_itmo"])
        # Same department rule as everyone: the department of their latest publication.
        self.assertEqual(authors["E1"]["dept"], authors["A1"]["dept"])
        self.assertEqual(authors["E1"]["pubs_count"], 1)
        self.assertEqual([p["key"] for p in summary["pubs"]], ["P1"])
        self.assertEqual(summary["pubs"][0]["n_authors"], 2)
        self.assertCountEqual(summary["all_edges"], [{"s": "A1", "t": "P1"}, {"s": "E1", "t": "P1"}])
        self.assertEqual({a["key"] for a in detail["authors"]}, {"A1", "E1"})

    def test_summary_label_is_the_full_form_not_the_truncated_public_one(self):
        # graph-data.json is one shared file across build variants (see
        # builder.py on public/private being decided by file location,
        # not content) - the map label currently uses author_label(...,
        # public=False): full surname ("Иванов", not the old "Ива..") and,
        # with no patronymic in this fixture, the full first name too (see
        # author_label()'s own force_initial=public branch, nodes.py) -
        # readability won out for now over the public-safe truncated form
        # (see nodes.py::AuthorNodeBuilder.build()).
        summary, _detail = GraphDataBuilder(self._sample_db(), seed=1).build()
        self.assertEqual(summary["authors"][0]["label"], "Иванов Иван")

    def test_summary_label_en_falls_back_to_name_en_when_split_en_name_parts_are_missing(self):
        # The real snapshot has surname_en/first_name_en/second_name_en as
        # None for every single person (no pipeline stage ever populates
        # them) - author_label() then always returns "", and without a
        # name_en fallback label_en silently became identical to label_ru
        # for 100% of authors regardless of the selected UI language (the
        # actual bug report: choosing English didn't make names English
        # anywhere except the panel card, which reads AuthorDetail.name_en
        # separately once it has merged in).
        db = self._sample_db()
        db["persons"][0]["surname_en"] = None
        db["persons"][0]["first_name_en"] = None
        db["persons"][0]["second_name_en"] = None
        db["persons"][0]["name_en"] = "Ivan Ivanov"
        summary, _detail = GraphDataBuilder(db, seed=1).build()
        self.assertEqual(summary["authors"][0]["label_en"], "Ivan Ivanov")

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


class WriteSiteDataTest(unittest.TestCase):
    def test_personal_author_detail_only_goes_to_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot.json"
            write_snapshot(snapshot, BuildGraphDataIntegrationTest._sample_db())
            write_site_data(snapshot, Path(tmp) / "gui", seed=1)

            public = {p.name for p in (Path(tmp) / "gui" / "public").iterdir()}
            private = {p.name for p in (Path(tmp) / "gui" / "private").iterdir()}
            self.assertEqual(public, {"graph-data.json", "repos-detail.json", "pubs-detail.json"})
            self.assertEqual(private, public | {"authors-detail.json"})
            graph = json.loads((Path(tmp) / "gui" / "private" / "graph-data.json").read_text(encoding="utf-8"))
            self.assertEqual([a["key"] for a in graph["authors"]], ["A1"])
