import unittest

import mongomock

from pauk.storage import RawStore


class RawStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_append_then_read_returns_the_row(self):
        store = RawStore(self.db, "sample")
        store.append("openalex_works", {"id": "W1"}, {"work_id": "W1"})
        rows = list(store.read("openalex_works"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "openalex_works")
        self.assertEqual(rows[0]["payload"], {"id": "W1"})
        self.assertEqual(rows[0]["request"], {"work_id": "W1"})
        self.assertIn("fetched_at", rows[0])

    def test_read_is_scoped_to_its_own_group(self):
        RawStore(self.db, "group-a").append("openalex_works", {"id": "W1"}, {})
        other = RawStore(self.db, "group-b")
        self.assertEqual(list(other.read("openalex_works")), [])

    def test_append_twice_keeps_both_rows_in_order(self):
        store = RawStore(self.db, "sample")
        store.append("openalex_works", {"id": "W1", "v": 1}, {})
        store.append("openalex_works", {"id": "W1", "v": 2}, {})
        rows = list(store.read("openalex_works"))
        self.assertEqual([row["payload"]["v"] for row in rows], [1, 2])

    def test_read_missing_source_yields_nothing(self):
        store = RawStore(self.db, "sample")
        self.assertEqual(list(store.read("nonexistent")), [])
