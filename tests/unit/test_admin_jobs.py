import re
import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import deps
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user
from pauk.jobs import store
from pauk.jobs.models import GRAPH, JobKind, JobState
from pauk.settings import Settings
from pauk.storage.naming import group_name
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
        self.assertIn("Задач ещё не было", page.text)

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


class SchedulingTest(unittest.TestCase):
    """Putting a run in the queue from the panel.

    The form writes a document and nothing else: the worker does the work,
    so there is no ordering to get wrong here — the run was either asked
    for or it was not.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.db.publications.insert_one({"id": "W1", "groups": ["2024"]})
        create_user(self.db, "chief", "hunter2", role="admin")
        create_user(self.db, "petrov", "hunter2", role="editor")
        create_user(self.db, "ivanov", "hunter2", role="viewer")
        self.app = build(Settings(), self.db)
        self.app.dependency_overrides[deps.graph_for] = lambda: FakePanelGraph()
        self.client, self.csrf = self.sign_in("chief")

    def sign_in(self, login):
        client = TestClient(self.app, follow_redirects=False)
        client.post("/login", data={"login": login, "password": "hunter2"})
        csrf = self.db[SESSIONS].find_one({"_id": client.cookies[COOKIE]})["csrf"]
        return client, csrf

    def post(self, client=None, csrf=None, **data):
        client = client or self.client
        return client.post("/jobs", data={"csrf": csrf or self.csrf, **data})

    def test_an_admin_can_publish(self):
        self.assertEqual(self.post(kind="publish", group="2024").status_code, 303)
        self.assertEqual(store.count(self.db), 1)

    def test_the_job_records_who_asked(self):
        self.post(kind="publish", group="2024")
        self.assertEqual(store.recent(self.db)[0].actor, "user:chief")

    def test_an_editor_may_not_start_a_run(self):
        # Editing one record is a change somebody can look at and undo; a
        # publish rewrites the whole graph.
        client, csrf = self.sign_in("petrov")
        self.assertEqual(self.post(client, csrf, kind="dedup").status_code, 403)
        self.assertEqual(store.count(self.db), 0)

    def test_a_viewer_may_not_either(self):
        client, csrf = self.sign_in("ivanov")
        self.assertEqual(self.post(client, csrf, kind="dedup").status_code, 403)

    def test_only_an_admin_is_offered_the_forms(self):
        self.assertIn("Запустить", self.client.get("/jobs").text)
        client, _ = self.sign_in("petrov")
        self.assertNotIn("Запустить", client.get("/jobs").text)

    def test_a_forged_form_is_refused(self):
        self.assertEqual(self.post(csrf="not-the-token", kind="dedup").status_code, 403)
        self.assertEqual(store.count(self.db), 0)

    def test_an_unknown_kind_is_refused(self):
        response = self.post(kind="rm -rf")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(store.count(self.db), 0)

    def test_a_group_without_prepared_rows_is_refused(self):
        # The form offers a list; a request that never met the form has to
        # meet the same list. Publishing an empty group takes the graph
        # lock to load nothing.
        response = self.post(kind="publish", group="2025")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(store.count(self.db), 0)

    def test_a_group_name_that_could_not_exist_is_refused(self):
        self.assertEqual(self.post(kind="publish", group="../etc").status_code, 400)

    def test_the_group_offered_is_one_that_has_rows(self):
        self.assertIn(">2024</option>", self.client.get("/jobs").text)

    def test_collecting_one_work(self):
        self.assertEqual(self.post(kind="collect", work_id="W123").status_code, 303)
        self.assertEqual(store.recent(self.db)[0].payload["work_id"], "W123")

    def test_collecting_a_period(self):
        self.post(kind="collect", date_from="2024-01-01", date_to="2024-12-31")
        self.assertEqual(store.recent(self.db)[0].payload["date_to"], "2024-12-31")

    def test_the_group_of_a_collection_run_is_derived_not_typed(self):
        # group_name is what `pauk run` uses; a second naming rule here
        # would drift from it.
        self.post(kind="collect", work_id="W123")
        group = store.recent(self.db)[0].payload["group"]
        self.assertEqual(group, group_name(work_id="W123"))

    def test_a_collection_run_holds_only_its_group(self):
        self.post(kind="collect", work_id="W123")
        self.assertTrue(store.recent(self.db)[0].resource.startswith("group:"))

    def test_both_a_work_and_a_period_is_refused(self):
        response = self.post(kind="collect", work_id="W1",
                             date_from="2024-01-01", date_to="2024-02-01")
        self.assertEqual(response.status_code, 400)

    def test_neither_a_work_nor_a_period_is_refused(self):
        self.assertEqual(self.post(kind="collect").status_code, 400)

    def test_a_period_the_wrong_way_round_is_refused(self):
        response = self.post(kind="collect", date_from="2024-12-31", date_to="2024-01-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("позже", response.json()["detail"])

    def test_something_that_is_not_a_date_says_so(self):
        # Told apart from the wrong order: one message for both would be
        # wrong half the time.
        response = self.post(kind="collect", date_from="вчера", date_to="2024-01-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("не дата", response.json()["detail"])

    def test_rebuilding_the_map(self):
        self.post(kind="map", seed="7", public="on")
        payload = store.recent(self.db)[0].payload
        self.assertEqual(payload, {"public": True, "seed": 7})

    def test_the_map_defaults_to_keeping_the_names(self):
        self.post(kind="map", seed="42")
        self.assertFalse(store.recent(self.db)[0].payload["public"])

    def test_deduplicating_takes_no_arguments(self):
        self.assertEqual(self.post(kind="dedup").status_code, 303)
        self.assertEqual(store.recent(self.db)[0].payload, {})

    def test_a_queued_run_is_reported_back(self):
        response = self.post(kind="dedup")
        self.assertIn("queued=", response.headers["location"])
        self.assertIn("поставлена в очередь",
                      self.client.get(response.headers["location"]).text)

    def test_nothing_is_started_by_the_request_itself(self):
        # The whole point of the queue: the request writes a document and
        # returns, and the worker does the rest.
        self.post(kind="publish", group="2024")
        self.assertEqual(store.recent(self.db)[0].state, JobState.QUEUED)
