import unittest

import mongomock

from pauk.storage.mongo import ensure_compression, ensure_indexes


class EnsureIndexesTest(unittest.TestCase):
    def test_creates_revisions_lookup_index(self):
        db = mongomock.MongoClient()["pauk_test"]
        ensure_indexes(db)
        index_keys = {tuple(info["key"]) for info in db.revisions.index_information().values()}
        self.assertIn(
            (("entity_type", 1), ("entity_id", 1), ("version", 1)),
            index_keys,
        )

    def test_creates_raw_read_indexes(self):
        db = mongomock.MongoClient()["pauk_test"]
        ensure_indexes(db)
        index_keys = {tuple(info["key"]) for info in db.raw.index_information().values()}
        self.assertIn((("source", 1), ("group", 1), ("fetched_at", 1)), index_keys)
        self.assertIn((("source", 1), ("fetched_at", 1)), index_keys)


class CompressionTest(unittest.TestCase):
    """The two heavy collections are asked for compressed, once, at creation.

    `raw` keeps a verbatim copy of every API answer and `revisions` a full
    snapshot per changed row; between them they are most of a working
    database, and both are repetitive JSON.
    """

    class Recorder:
        """A database that records what it was asked to create."""

        def __init__(self, existing=()):
            self.existing = list(existing)
            self.made: list[tuple[str, dict]] = []

        def list_collection_names(self):
            return self.existing

        def create_collection(self, name, **options):
            self.made.append((name, options))

    def test_a_fresh_database_gets_both_compressed(self):
        db = self.Recorder()
        self.assertEqual(ensure_compression(db), ["raw", "revisions"])
        for _name, options in db.made:
            self.assertIn("zstd", options["storageEngine"]["wiredTiger"]["configString"])

    def test_a_collection_that_exists_is_left_alone(self):
        # WiredTiger takes the compressor when the collection is made.
        # Changing it afterwards needs collMod and a rewrite, which holds a
        # lock — an operator's decision, not a thing to do on startup.
        db = self.Recorder(["raw", "revisions"])
        self.assertEqual(ensure_compression(db), [])
        self.assertEqual(db.made, [])

    def test_only_the_heavy_ones(self):
        db = self.Recorder()
        ensure_compression(db)
        self.assertEqual([name for name, _ in db.made], ["raw", "revisions"])

    def test_a_server_that_will_not_take_the_option_is_not_an_error(self):
        # An old server, an account without the right, a double that does
        # not implement it. The collection appears on the first write with
        # the default compressor, which is worse and still works.
        class Refuses(self.Recorder):
            def create_collection(self, name, **options):
                raise NotImplementedError("Special options not supported")

        self.assertEqual(ensure_compression(Refuses()), [])

    def test_the_indexes_still_get_made_on_a_double(self):
        db = mongomock.MongoClient()["pauk_test"]
        ensure_indexes(db)
        self.assertTrue(db.raw.index_information())
