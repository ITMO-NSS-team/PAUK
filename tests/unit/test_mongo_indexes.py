import unittest

import mongomock

from pauk.storage.mongo import ensure_indexes


class EnsureIndexesTest(unittest.TestCase):
    def test_creates_revisions_lookup_index(self):
        db = mongomock.MongoClient()["pauk_test"]
        ensure_indexes(db)
        index_keys = {tuple(info["key"]) for info in db.revisions.index_information().values()}
        self.assertIn(
            (("entity_type", 1), ("entity_id", 1), ("version", 1)),
            index_keys,
        )
