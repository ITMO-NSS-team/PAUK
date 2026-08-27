import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import feed
from pauk.admin.app import build
from pauk.admin.auth import create_user
from pauk.settings import Settings
from tests.unit.test_admin_nodes import FakePanelGraph


def entry(**over):
    row = {"timestamp": "2026-08-22T10:00:00", "actor": "user:roman", "source": "admin-ui",
           "operation": "upsert_nodes", "entity_type": "Person", "entity_id": "A1",
           "change_kind": "updated", "diff": {"name_ru": ["Иван", "Пётр"]}}
    row.update(over)
    return row


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.db[feed.COLLECTION].insert_many([
            entry(timestamp="2026-08-22T10:00:00"),
            entry(timestamp="2026-08-22T11:00:00", actor="pipeline", source="publish",
                  diff={"name_ru": ["Пётр", "Иван"]}),
            entry(timestamp="2026-08-22T09:00:00", entity_id="A2", change_kind="created"),
            entry(timestamp="2026-08-21T09:00:00", entity_type="Repository", entity_id="R1",
                  actor="user:guest", change_kind="deleted", diff={}),
        ])

    def test_the_newest_change_comes_first(self):
        rows = feed.entries(self.db)
        self.assertEqual(rows[0]["timestamp"], "2026-08-22T11:00:00")
        self.assertEqual(rows[-1]["timestamp"], "2026-08-21T09:00:00")

    def test_filtering_by_who_made_the_change(self):
        self.assertEqual(len(feed.entries(self.db, actor="pipeline")), 1)
        self.assertEqual(len(feed.entries(self.db, actor="user:roman")), 2)

    def test_filtering_by_entity_and_by_kind(self):
        self.assertEqual(len(feed.entries(self.db, entity_type="Repository")), 1)
        self.assertEqual(len(feed.entries(self.db, entity_id="A1")), 2)
        self.assertEqual(len(feed.entries(self.db, kind="created")), 1)

    def test_one_entity_history_holds_both_the_person_and_the_pipeline(self):
        # The point of the feed: a field a person edits and a publish puts
        # back is only visible when both are in one list.
        rows = feed.history(self.db, "Person", "A1")
        self.assertEqual([row["actor"] for row in rows], ["pipeline", "user:roman"])

    def test_paging_does_not_repeat_or_skip_rows(self):
        first = feed.entries(self.db, limit=2)
        second = feed.entries(self.db, limit=2, skip=2)
        self.assertEqual(len(first) + len(second), 4)
        self.assertFalse({row["timestamp"] for row in first} & {row["timestamp"] for row in second})

    def test_changes_are_pairs_in_a_stable_order(self):
        self.db[feed.COLLECTION].insert_one(
            entry(entity_id="A9", diff={"b": [1, 2], "a": [3, 4]}))
        (row,) = feed.entries(self.db, entity_id="A9")
        self.assertEqual([name for name, _ in row["changes"]], ["a", "b"])

    def test_the_kind_is_shown_in_words(self):
        (row,) = feed.entries(self.db, kind="deleted")
        self.assertEqual(row["kind_ru"], "удалено")

    def test_the_filter_lists_come_from_what_is_actually_there(self):
        self.assertEqual(feed.actors(self.db), ["pipeline", "user:guest", "user:roman"])
        self.assertEqual(feed.entity_types(self.db), ["Person", "Repository"])


class AuditPageTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.db[feed.COLLECTION].insert_many([entry(), entry(actor="pipeline", source="publish")])
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1", "name_ru": "Пётр"}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})

    def test_the_feed_is_closed_without_a_session(self):
        self.assertEqual(self.client.get("/audit").status_code, 401)

    def test_the_feed_shows_who_changed_what(self):
        self.sign_in()
        body = self.client.get("/audit").text
        self.assertIn("user:roman", body)
        self.assertIn("pipeline", body)
        self.assertIn("name_ru", body)
        self.assertIn("Пётр", body)

    def test_a_viewer_can_read_the_feed(self):
        # Reading who changed what is not a privilege — it is how a wrong
        # value gets explained.
        self.sign_in(login="guest")
        self.assertEqual(self.client.get("/audit").status_code, 200)

    def test_filters_narrow_the_list(self):
        # Checked inside the table: every actor also appears in the filter
        # dropdown, so searching the whole page proves nothing.
        self.sign_in()
        body = self.client.get("/audit", params={"actor": "pipeline"}).text
        table = body.split("<table>")[1].split("</table>")[0]
        self.assertIn("pipeline", table)
        self.assertNotIn("user:roman", table)
        self.assertIn("Всего: 1", body)

    def test_an_empty_feed_says_so(self):
        self.db[feed.COLLECTION].delete_many({})
        self.sign_in()
        self.assertIn("Записей нет", self.client.get("/audit").text)

    def test_the_node_page_shows_its_own_history(self):
        self.sign_in()
        body = self.client.get("/nodes/Person/A1").text
        self.assertIn("Что с ней делали", body)
        self.assertIn("user:roman", body)
        self.assertIn("/audit?entity_type=Person&entity_id=A1", body)

    def test_the_feed_is_a_section_in_the_header(self):
        self.sign_in()
        self.assertIn('href="/audit"', self.client.get("/").text)

    def test_the_current_section_is_marked(self):
        self.sign_in()
        header = self.client.get("/audit").text.split("<header>")[1].split("</header>")[0]
        self.assertIn("section on", header)

    def test_the_filters_are_laid_out_as_wide_as_the_field_beside_them(self):
        # They used to shrink to their own text and looked smaller than the
        # id input next to them.
        self.sign_in()
        self.assertIn('class="filters"', self.client.get("/audit").text)


if __name__ == "__main__":
    unittest.main()


class DeletedNodeTest(unittest.TestCase):
    """Links in the feed outlive the nodes they point at."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.db[feed.COLLECTION].insert_many([
            entry(entity_type="Publication", entity_id="W9", change_kind="created",
                  timestamp="2026-08-21T15:08:48", diff={"id": [None, "W9"]}),
            entry(entity_type="Publication", entity_id="W9", change_kind="deleted",
                  timestamp="2026-08-21T15:08:57", diff={"id": ["W9", None]}),
        ])
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1"}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def test_a_deleted_node_explains_itself_instead_of_a_bare_404(self):
        # Arriving from the feed, the question is "what happened to it",
        # and the feed is exactly where the answer is.
        response = self.client.get("/nodes/Publication/W9")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Этой записи в графе нет", response.text)
        self.assertIn("удалено", response.text)
        self.assertIn("user:roman", response.text)

    def test_an_id_nobody_ever_touched_is_a_plain_404(self):
        response = self.client.get("/nodes/Publication/never-existed")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Этой записи в графе нет", response.text)

    def test_an_editor_is_offered_the_record_back(self):
        # One button, nothing around it: what comes back and what happens
        # to the tombstone is already visible in the history above.
        body = self.client.get("/nodes/Publication/W9").text
        self.assertIn("/nodes/Publication/restore/W9", body)
        self.assertIn("Восстановить", body)
        self.assertNotIn("overrides undo", body)

    def test_a_live_node_still_opens_normally(self):
        self.assertEqual(self.client.get("/nodes/Person/A1").status_code, 200)


class RestoreTest(unittest.TestCase):
    """Bringing a deleted node back, and what happens to its tombstone."""

    def setUp(self):
        from pauk.graph.overrides import record_override
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.db[feed.COLLECTION].insert_one(
            entry(entity_type="LinkCandidate", entity_id="L1", change_kind="deleted",
                  diff={"id": ["L1", None], "url": ["https://x.test", None],
                        "host": ["x.test", None]}))
        record_override(self.db, "LinkCandidate", "L1", "delete", actor="user:roman")
        self.graph = FakePanelGraph()

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})
        from pauk.admin.auth import COOKIE, SESSIONS
        return self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

    def test_the_node_comes_back_with_the_fields_it_had(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/LinkCandidate/restore/L1", data={"csrf": csrf})
        self.assertEqual(response.status_code, 303)
        node = self.graph.nodes[("LinkCandidate", "L1")]
        self.assertEqual(node["url"], "https://x.test")
        self.assertEqual(node["host"], "x.test")

    def test_restoring_withdraws_the_tombstone(self):
        # Without this the next publish would delete the node a second
        # time, and the restore would look like it silently failed.
        from pauk.graph.overrides import active_overrides
        csrf = self.sign_in()
        self.client.post("/nodes/LinkCandidate/restore/L1", data={"csrf": csrf})
        self.assertEqual(active_overrides(self.db), [])

    def test_creating_the_same_id_by_hand_also_withdraws_it(self):
        # Typing the id into the create form says just as plainly that the
        # record is wanted.
        from pauk.graph.overrides import active_overrides
        csrf = self.sign_in()
        self.client.post("/nodes/LinkCandidate/new",
                         data={"csrf": csrf, "id": "L1", "url": "https://new.test"})
        self.assertEqual(active_overrides(self.db), [])

    def test_a_node_the_feed_cannot_describe_is_not_restorable(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/restore/never-seen", data={"csrf": csrf})
        self.assertEqual(response.status_code, 400)

    def test_an_entity_whose_last_event_was_not_a_deletion_is_left_alone(self):
        # Restoring then would overwrite something that is alive.
        self.db[feed.COLLECTION].insert_one(
            entry(entity_type="LinkCandidate", entity_id="L1", change_kind="created",
                  timestamp="2026-08-22T12:00:00", diff={"id": [None, "L1"]}))
        self.assertEqual(feed.deleted_state(self.db, "LinkCandidate", "L1"), {})

    def test_a_viewer_cannot_restore(self):
        csrf = self.sign_in(login="guest")
        response = self.client.post("/nodes/LinkCandidate/restore/L1", data={"csrf": csrf})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(("LinkCandidate", "L1"), self.graph.nodes)

    def test_restoring_without_the_csrf_token_is_refused(self):
        self.sign_in()
        response = self.client.post("/nodes/LinkCandidate/restore/L1")
        self.assertEqual(response.status_code, 403)

    def test_the_page_offers_the_restore(self):
        self.sign_in()
        body = self.client.get("/nodes/LinkCandidate/L1").text
        self.assertIn("/nodes/LinkCandidate/restore/L1", body)
        self.assertIn("Восстановить", body)

    def test_a_viewer_is_shown_no_button(self):
        self.sign_in(login="guest")
        self.assertNotIn("/restore", self.client.get("/nodes/LinkCandidate/L1").text)
