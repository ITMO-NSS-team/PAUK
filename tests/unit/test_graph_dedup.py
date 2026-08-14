import unittest

import mongomock

from pauk.graph.dedup import collect_raw_orcids
from pauk.storage import RawStore


class CollectRawOrcidsTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_later_fetch_overrides_earlier_across_groups(self):
        RawStore(self.db, "group-a").append(
            "openalex_authors",
            {"id": "https://openalex.org/A1", "orcid": "https://orcid.org/0000-0001"}, {})
        RawStore(self.db, "group-b").append(
            "openalex_authors",
            {"id": "https://openalex.org/A1", "orcid": "https://orcid.org/0000-0002"}, {})
        orcids = collect_raw_orcids(self.db)
        self.assertEqual(orcids["A1"], "0000-0002")

    def test_missing_orcid_is_none(self):
        RawStore(self.db, "group-a").append(
            "openalex_authors", {"id": "https://openalex.org/A1"}, {})
        orcids = collect_raw_orcids(self.db)
        self.assertIsNone(orcids["A1"])

    def test_ignores_other_sources(self):
        RawStore(self.db, "group-a").append(
            "openalex_works", {"id": "https://openalex.org/W1"}, {})
        self.assertEqual(collect_raw_orcids(self.db), {})


if __name__ == "__main__":
    unittest.main()
