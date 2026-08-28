import re
import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import deps
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user
from pauk.jobs import store
from pauk.jobs.models import GRAPH, JobKind
from pauk.settings import Settings
from tests.unit.test_admin_nodes import FakePanelGraph


class JobsPageTest(unittest.TestCase):
    """The queue and the history, read-only.

    Starting a run is a later step; until then the page exists so that a
    person looking at a field can tell whether a publish is rewriting it.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_ru="Иван Петров")
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def finished(self, kind=JobKind.PUBLISH, payload=None, result=None, actor="user:roman"):
        job = store.enqueue(self.db, kind, payload or {"group": "2024"}, actor=actor)
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        store.finish(self.db, job.id, result or {"rows_persons": 12})
        return job

    def under_way(self, kind=JobKind.MAP, payload=None):
        job = store.enqueue(self.db, kind, payload or {"public": True})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        return job

    def test_an_empty_page_says_so(self):
        page = self.client.get("/jobs")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Задач нет", page.text)

    def test_a_finished_run_shows_its_counts(self):
        self.finished(result={"rows_persons": 12})
        page = self.client.get("/jobs").text
        self.assertIn("rows_persons", page)
        self.assertIn("12", page)

    def test_a_failed_run_shows_why(self):
        job = store.enqueue(self.db, JobKind.DEDUP, {})
        store.fail(self.db, job.id, "ServiceUnavailable: нет связи")
        self.assertIn("нет связи", self.client.get("/jobs").text)

    def test_a_run_under_way_is_shown_apart(self):
        # Above the history and not in date order: a run in progress is
        # what the page is opened for, and a later job may already be done.
        self.under_way()
        page = self.client.get("/jobs").text
        self.assertIn("Сейчас", page)
        self.assertIn("пересборка карты", page)

    def test_the_kinds_are_named_in_words(self):
        self.finished(kind=JobKind.PUBLISH)
        self.assertIn("публикация", self.client.get("/jobs").text)

    def test_the_history_can_be_filtered_by_state(self):
        self.finished()
        job = store.enqueue(self.db, JobKind.DEDUP, {})
        store.fail(self.db, job.id, "boom")
        page = self.client.get("/jobs", params={"state": "failed"}).text
        self.assertIn("boom", page)
        self.assertNotIn("rows_persons", page)

    def test_the_history_can_be_filtered_by_who_asked(self):
        self.finished(actor="user:roman")
        self.finished(actor="user:petrov", result={"rows_departments": 3})
        page = self.client.get("/jobs", params={"actor": "user:petrov"}).text
        self.assertIn("rows_departments", page)
        self.assertNotIn("rows_persons", page)

    def test_the_pager_keeps_the_filter(self):
        for _ in range(store.PAGE + 5):
            self.finished(actor="user:roman")
        page = self.client.get("/jobs", params={"actor": "user:roman"}).text
        link = re.search(r'href="(/jobs\?page=2[^"]*)"', page)
        self.assertIsNotNone(link, "нет ссылки на вторую страницу")
        self.assertIn("actor=user%3Aroman", link.group(1))

    def test_a_viewer_may_read_it(self):
        # Whether a publish is under way explains what somebody is looking
        # at; that is not a privilege.
        create_user(self.db, "petrov", "hunter2", role="viewer")
        client = TestClient(build(Settings(), self.db), follow_redirects=False)
        client.post("/login", data={"login": "petrov", "password": "hunter2"})
        self.assertEqual(client.get("/jobs").status_code, 200)

    def test_signing_in_is_required(self):
        client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.assertEqual(client.get("/jobs", headers={"accept": "application/json"}).status_code,
                         401)

    def test_the_page_is_linked_from_every_screen(self):
        self.assertIn('href="/jobs"', self.client.get("/").text)


class GraphBusyBannerTest(unittest.TestCase):
    """The strip that warns an editor, on whatever page they are on.

    Decided deliberately: warn, do not block. The edit goes through, and if
    the publish covers it the disagreement shows up on the decisions screen.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_ru="Иван Петров")
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

    def start(self, kind=JobKind.PUBLISH, payload=None):
        job = store.enqueue(self.db, kind, payload or {"group": "2024"})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        return job

    def test_nothing_is_shown_when_the_graph_is_free(self):
        self.assertNotIn("граф сейчас переписывается", self.client.get("/nodes/Person/A1").text)

    def test_it_warns_on_a_node_page(self):
        self.start()
        self.assertIn("граф сейчас переписывается", self.client.get("/nodes/Person/A1").text)

    def test_it_names_the_kind_of_run(self):
        self.start(kind=JobKind.MAP, payload={})
        self.assertIn("Идёт пересборка карты", self.client.get("/nodes/Person/A1").text)

    def test_it_shows_up_on_pages_that_know_nothing_about_jobs(self):
        # The strip lives in the layout, so a screen written before jobs
        # existed warns too.
        self.start()
        for path in ("/", "/audit", "/overrides", "/nodes/Person"):
            with self.subTest(path=path):
                self.assertIn("граф сейчас переписывается", self.client.get(path).text)

    def test_a_collection_run_does_not_warn(self):
        # It holds its group, not the graph. Editing a node is unaffected.
        self.start(kind=JobKind.COLLECT, payload={"group": "2024", "work_id": "W1"})
        self.assertNotIn("граф сейчас переписывается", self.client.get("/nodes/Person/A1").text)

    def test_it_goes_away_when_the_run_ends(self):
        job = self.start()
        store.finish(self.db, job.id, {})
        self.assertNotIn("граф сейчас переписывается", self.client.get("/nodes/Person/A1").text)

    def test_the_edit_still_goes_through(self):
        self.start()
        seen = self.graph.nodes[("Person", "A1")]["updated_at"]
        response = self.client.post("/nodes/Person/A1", data={
            "csrf": self.csrf, "name_ru": "Пётр Иванов", "seen_at": seen})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Пётр Иванов")

    def test_a_queue_that_cannot_be_read_costs_the_strip_not_the_page(self):
        from pymongo.errors import PyMongoError
        self.start()
        broken = self.db[store.COLLECTION].find

        def fail(*args, **kwargs):
            raise PyMongoError("the queue is unreadable")

        self.db[store.COLLECTION].find = fail
        try:
            page = self.client.get("/nodes/Person/A1")
        finally:
            self.db[store.COLLECTION].find = broken
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("граф сейчас переписывается", page.text)

    def test_it_is_the_graph_job_that_is_named(self):
        self.start(kind=JobKind.COLLECT, payload={"group": "2024", "work_id": "W1"})
        self.start(kind=JobKind.PUBLISH)
        page = self.client.get("/nodes/Person/A1").text
        self.assertIn("Идёт публикация", page)
        self.assertEqual(store.running(self.db, resource=GRAPH)[0].kind, JobKind.PUBLISH)
