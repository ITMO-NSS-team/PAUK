"""Reading the archive of what the source said about a record.

Every real change to a prepared row files the whole previous document. The
archive has been filling up since versioning landed and nothing could open
it: the only way to look was a query by hand.
"""

import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import deps, source
from pauk.admin.app import build
from pauk.admin.auth import create_user
from pauk.settings import Settings
from pauk.storage import PreparedStore
from tests.unit.test_admin_nodes import FakePanelGraph


def archived(entity, entity_id, version, snapshot, group, when):
    return {"entity_type": entity, "entity_id": entity_id, "version": version,
            "snapshot": snapshot, "replaced_by_group": group, "replaced_at": when}


class HistoryTest(unittest.TestCase):
    """A change is the gap between two versions, not a version itself."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.store = PreparedStore(self.db, "период-3")

    def fill(self):
        self.db[source.REVISIONS].insert_many([
            archived("persons", "A1", 1, {"id": "A1", "name_raw": "Ivan"},
                     "период-1", "2026-06-01T10:00:00"),
            archived("persons", "A1", 2, {"id": "A1", "name_raw": "Ivan", "orcid": "0000-1"},
                     "период-2", "2026-07-01T10:00:00"),
        ])
        self.store.write_rows("persons", [
            {"id": "A1", "name_raw": "Ivan Smirnov", "orcid": "0000-1"}])

    def test_a_record_nothing_ever_replaced_has_no_history(self):
        self.store.write_rows("persons", [{"id": "A1", "name_raw": "Ivan"}])
        self.assertEqual(source.history(self.db, "Person", "A1"), [])

    def test_each_row_is_one_run_and_what_it_changed(self):
        self.fill()
        rows = source.history(self.db, "Person", "A1")
        self.assertEqual([row["group"] for row in rows], ["период-2", "период-1"])

    def test_the_newest_change_is_against_the_row_as_it_stands(self):
        # Without the live row the last change — the one somebody is
        # usually asking about — would be the one missing.
        self.fill()
        newest = source.history(self.db, "Person", "A1")[0]
        self.assertEqual(newest["changes"], [("name_raw", ("Ivan", "Ivan Smirnov"))])

    def test_an_older_change_is_against_the_version_that_replaced_it(self):
        self.fill()
        older = source.history(self.db, "Person", "A1")[1]
        self.assertEqual(older["changes"], [("orcid", (None, "0000-1"))])

    def test_bookkeeping_fields_are_not_changes(self):
        # _version moves on every write and groups on every claim; neither
        # is something a run decided about the record.
        self.db[source.REVISIONS].insert_one(
            archived("persons", "A2", 1,
                     {"id": "A2", "name_raw": "Anna", "_version": 1, "groups": ["период-1"]},
                     "период-2", "2026-07-01T10:00:00"))
        self.store.write_rows("persons", [{"id": "A2", "name_raw": "Anna"}])
        self.assertEqual(source.history(self.db, "Person", "A2")[0]["changes"], [])

    def test_a_list_is_counted_rather_than_printed(self):
        # A person's publications change on most runs. Printed whole they
        # bury the one field somebody came to look at.
        self.db[source.REVISIONS].insert_one(
            archived("persons", "A3", 1, {"id": "A3", "authored": [1, 2]},
                     "период-2", "2026-07-01T10:00:00"))
        self.store.write_rows("persons", [{"id": "A3", "authored": [1, 2, 3]}])
        self.assertEqual(source.history(self.db, "Person", "A3")[0]["changes"],
                         [("authored", ("2 элем.", "3 элем."))])

    def test_a_label_with_no_prepared_rows_has_no_archive(self):
        # LinkCandidate is invented from repo_links rows, not published
        # from any of its own.
        self.assertEqual(source.history(self.db, "LinkCandidate", "L1"), [])
        self.assertEqual(source.count(self.db, "LinkCandidate", "L1"), 0)

    def test_only_the_newest_are_read(self):
        self.db[source.REVISIONS].insert_many([
            archived("persons", "A4", version, {"id": "A4", "name_raw": f"v{version}"},
                     f"период-{version}", f"2026-0{version}-01T10:00:00")
            for version in range(1, 6)])
        rows = source.history(self.db, "Person", "A4", limit=2)
        self.assertEqual([row["group"] for row in rows], ["период-5", "период-4"])
        self.assertEqual(source.count(self.db, "Person", "A4"), 5)


class HistoryOnThePageTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_en="Ivan Smirnov")
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def body(self):
        return self.client.get("/nodes/Person/A1").text

    def test_the_block_is_there_even_with_nothing_in_it(self):
        # The absence is an answer too: nobody has to wonder whether the
        # panel simply does not know.
        self.assertIn("Что говорил источник", self.body())

    def test_a_run_that_changed_the_record_is_shown(self):
        self.db[source.REVISIONS].insert_one(
            archived("persons", "A1", 1, {"id": "A1", "orcid": None},
                     "2026-08-30__from_2026-03-01", "2026-08-30T12:00:00"))
        PreparedStore(self.db, "sample").write_rows(
            "persons", [{"id": "A1", "orcid": "0000-0002"}])
        body = self.body()
        self.assertIn("2026-08-30__from_2026-03-01", body)
        self.assertIn("0000-0002", body)

    def test_it_is_not_the_same_block_as_the_graph_journal(self):
        # Two questions about one record: what people did to the graph, and
        # what the pipeline decided about the source.
        body = self.body()
        self.assertIn("Что с ней делали", body)
        self.assertIn("Что говорил источник", body)

    def test_a_viewer_sees_it_too(self):
        create_user(self.db, "guest", "hunter2", role="viewer")
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"login": "guest", "password": "hunter2"})
        self.assertIn("Что говорил источник", client.get("/nodes/Person/A1").text)
