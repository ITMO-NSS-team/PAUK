import unittest

import mongomock

from pauk.models import Publication
from pauk.storage import PreparedStore


class PreparedStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_write_then_read_round_trip(self):
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper")])
        rows = list(store.read_models("publications", Publication))
        self.assertEqual([r.id for r in rows], ["W1"])
        self.assertEqual(rows[0].title, "Paper")

    def test_read_is_scoped_to_its_own_group(self):
        PreparedStore(self.db, "group-a").write_models("publications", [Publication(id="W1", title="Paper")])
        other = PreparedStore(self.db, "group-b")
        self.assertEqual(list(other.read_models("publications", Publication)), [])

    def test_get_models_finds_a_document_regardless_of_group(self):
        PreparedStore(self.db, "group-a").write_models(
            "publications", [Publication(id="W1", title="Paper", has_code=True)]
        )
        store_b = PreparedStore(self.db, "group-b")
        # group-b never touched W1, so the group-scoped read doesn't see it...
        self.assertEqual(list(store_b.read_models("publications", Publication)), [])
        # ...but a targeted lookup by id crosses the group boundary.
        [found] = list(store_b.get_models("publications", ["W1"], Publication))
        self.assertEqual(found.id, "W1")
        self.assertTrue(found.has_code)

    def test_get_models_skips_unknown_ids_without_erroring(self):
        store = PreparedStore(self.db, "sample")
        self.assertEqual(list(store.get_models("publications", ["ghost"], Publication)), [])

    def test_second_group_writing_a_row_adds_provenance_without_hiding_it_from_the_first(self):
        PreparedStore(self.db, "group-a").write_models(
            "publications", [Publication(id="W1", title="Paper", has_code=True)]
        )
        store_b = PreparedStore(self.db, "group-b")
        [existing] = list(store_b.get_models("publications", ["W1"], Publication))
        existing.abstract = "seen by group b too"
        store_b.write_models("publications", [existing])

        [seen_by_a] = list(PreparedStore(self.db, "group-a").read_models("publications", Publication))
        self.assertTrue(seen_by_a.has_code)
        self.assertEqual(seen_by_a.abstract, "seen by group b too")
        [seen_by_b] = list(store_b.read_models("publications", Publication))
        self.assertEqual(seen_by_b.id, "W1")

    def test_write_rows_round_trips_plain_dicts_keyed_by_publication_id(self):
        # RepoLink has no "id" field of its own - it's keyed by publication_id.
        store = PreparedStore(self.db, "sample")
        store.write_rows("repo_links", [{"publication_id": "W1", "links": []}])
        rows = list(store.read_rows("repo_links"))
        self.assertEqual(rows, [{"publication_id": "W1", "links": []}])

    def test_first_write_sets_version_to_one_and_creates_no_revision(self):
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper")])
        self.assertEqual(self.db.publications.find_one({"_id": "W1"})["_version"], 1)
        self.assertEqual(self.db.revisions.count_documents({}), 0)

    def test_rewriting_identical_content_does_not_bump_version_or_create_revision(self):
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper")])
        store.write_models("publications", [Publication(id="W1", title="Paper")])
        self.assertEqual(self.db.publications.find_one({"_id": "W1"})["_version"], 1)
        self.assertEqual(self.db.revisions.count_documents({}), 0)

    def test_clearing_a_field_to_none_actually_removes_it(self):
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper", journal="Old Journal")])
        [row] = list(store.read_models("publications", Publication))
        self.assertEqual(row.journal, "Old Journal")

        row.journal = None
        store.write_models("publications", [row])
        [row] = list(store.read_models("publications", Publication))
        self.assertIsNone(row.journal)

    def test_writing_changed_content_bumps_version_and_archives_old_snapshot(self):
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper")])
        store.write_models("publications", [Publication(id="W1", title="Paper v2")])

        current = self.db.publications.find_one({"_id": "W1"})
        self.assertEqual(current["_version"], 2)
        self.assertEqual(current["title"], "Paper v2")

        [revision] = list(self.db.revisions.find({}))
        self.assertEqual(revision["entity_type"], "publications")
        self.assertEqual(revision["entity_id"], "W1")
        self.assertEqual(revision["version"], 1)
        self.assertEqual(revision["snapshot"]["title"], "Paper")
        self.assertEqual(revision["replaced_by_group"], "sample")

    def test_legacy_document_without_version_gets_backfilled_with_no_revision(self):
        # Simulates a document written before _version existed: same content
        # a real write_rows call would have produced, but no _version field.
        row = Publication(id="W1", title="Paper").model_dump(mode="json", by_alias=True, exclude_none=True)
        self.db.publications.insert_one({"_id": "W1", **row, "groups": ["sample"]})
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper")])

        self.assertEqual(self.db.publications.find_one({"_id": "W1"})["_version"], 1)
        self.assertEqual(self.db.revisions.count_documents({}), 0)

    def test_legacy_document_without_version_that_also_changed_gets_version_one_and_no_crash(self):
        row = Publication(id="W1", title="Paper").model_dump(mode="json", by_alias=True, exclude_none=True)
        self.db.publications.insert_one({"_id": "W1", **row, "groups": ["sample"]})
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper v2")])

        current = self.db.publications.find_one({"_id": "W1"})
        self.assertEqual(current["_version"], 1)
        self.assertEqual(current["title"], "Paper v2")
        [revision] = list(self.db.revisions.find({}))
        self.assertEqual(revision["snapshot"]["title"], "Paper")

    def test_version_field_is_not_leaked_through_read_rows(self):
        store = PreparedStore(self.db, "sample")
        store.write_models("publications", [Publication(id="W1", title="Paper")])
        [row] = list(store.read_rows("publications"))
        self.assertNotIn("_version", row)
