import unittest

import mongomock

from pauk.models import Person
from pauk.storage import RawStore
from pauk.storage.prepared import PreparedStore


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

    def test_point_upsert_does_not_remove_other_group_rows(self):
        prepared = PreparedStore(self.db, "sample")
        first = Person(id="A1", is_itmo=False)
        second = Person(id="A2", is_itmo=False)
        prepared.write_models("persons", [first, second])
        first.name_raw = "Saved immediately"
        prepared.upsert_models("persons", [first])
        self.assertEqual({row.id for row in prepared.read_models("persons", Person)}, {"A1", "A2"})
