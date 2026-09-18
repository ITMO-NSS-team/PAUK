"""The graph's health as a page in the panel.

The checks and the code that runs them are `pauk.admin.checks`/`graph_stats`. What is tested
here is the part that is the panel's: keeping the last answer, showing the
worst of it first, and going to the graph only for the rows behind one
check.
"""

import unittest
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import deps, health
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user, session_key
from pauk.jobs import store
from pauk.jobs.models import JobKind
from pauk.settings import Settings
from tests.unit.test_admin_nodes import FakePanelGraph


def check(check_id, group="Пропуски", status="ok", n=0, of=100, pct=0.0,
          title=None, examples=True):
    return {"id": check_id, "group": group, "group_en": group,
            "title": title or check_id, "title_en": check_id,
            "n": n, "of": of, "pct": pct, "status": status,
            "hint": None, "hint_en": None, "has_examples": examples}


class KeepingTheAnswerTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_nothing_kept_until_something_runs(self):
        self.assertIsNone(health.latest(self.db))

    def test_a_second_run_replaces_the_first(self):
        # One document, not a history: "what the graph looks like now" is
        # the question, and the queue already records when each run was.
        health.save(self.db, {"checks": [check("a")]})
        health.save(self.db, {"checks": [check("b")]})
        self.assertEqual(self.db[health.COLLECTION].count_documents({}), 1)
        self.assertEqual(health.latest(self.db)["stats"]["checks"][0]["id"], "b")

    def test_the_time_it_was_taken_is_kept_with_it(self):
        # A number without its date is worse than no number.
        saved = health.save(self.db, {"checks": []})
        self.assertTrue(saved["computed_at"])


class WorstFirstTest(unittest.TestCase):
    """Somebody opening the page is looking for what is wrong."""

    def test_a_group_opens_on_its_failures(self):
        rows = health.grouped([
            check("fine", status="ok"),
            check("broken", status="fail", n=10, pct=10.0),
            check("iffy", status="warn", n=5, pct=5.0),
        ])
        self.assertEqual([c["id"] for c in rows[0]["checks"]], ["broken", "iffy", "fine"])

    def test_a_check_that_did_not_run_comes_before_all_of_them(self):
        rows = health.grouped([check("broken", status="fail"), check("dead", status="error")])
        self.assertEqual([c["id"] for c in rows[0]["checks"]], ["dead", "broken"])

    def test_the_bigger_share_wins_within_one_state(self):
        rows = health.grouped([
            check("small", status="fail", n=3, pct=1.0),
            check("wide", status="fail", n=2, pct=40.0),
        ])
        self.assertEqual([c["id"] for c in rows[0]["checks"]], ["wide", "small"])

    def test_groups_keep_the_order_they_were_defined_in(self):
        rows = health.grouped([check("a", group="Пропуски"), check("b", group="Имена")])
        self.assertEqual([row["name"] for row in rows], ["Пропуски", "Имена"])

    def test_the_verdict_counts_every_state(self):
        counted = health.verdict([check("a", status="fail"), check("b", status="ok"),
                                  check("c", status="ok")])
        self.assertEqual((counted["fail"], counted["ok"], counted["warn"]), (1, 2, 0))


class WhatCanBeOpenedTest(unittest.TestCase):
    def test_a_check_with_rows_behind_it_can(self):
        self.assertTrue(health.openable(check("a", status="fail", n=4)))

    def test_one_sitting_at_zero_cannot(self):
        # There is nothing to show, and a link that opens an empty table
        # reads as a broken link.
        self.assertFalse(health.openable(check("a", n=0)))

    def test_nor_can_one_without_an_examples_query(self):
        self.assertFalse(health.openable(check("a", n=4, examples=False)))

    def test_nor_one_that_failed_to_run(self):
        # It would lead to the same error a second time.
        self.assertFalse(health.openable(check("a", status="error", n=4)))


class PageTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "chief", "hunter2", role="admin")
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.graph = FakePanelGraph()
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.app = app
        self.client = self.sign_in("chief")

    def sign_in(self, login):
        client = TestClient(self.app, follow_redirects=False)
        client.post("/login", data={"login": login, "password": "hunter2"})
        return client

    def csrf(self, client):
        return self.db[SESSIONS].find_one(
            {"_id": session_key(client.cookies[COOKIE])})["csrf"]

    def fill(self):
        health.save(self.db, {"totals": {"nodes": 640, "rels": 721}, "checks": [
            check("itmo_no_dept", status="fail", n=117, of=189, pct=61.9,
                  title="Сотрудники без департамента"),
            check("pub_no_abstract", status="ok", n=0, of=83, title="Публикации без аннотации"),
        ]})

    def test_it_is_in_the_header(self):
        self.assertIn("Здоровье БД", self.client.get("/").text)

    def test_an_empty_page_says_what_to_do(self):
        body = self.client.get("/health").text
        self.assertIn("Нажмите «Посчитать»", body)
        self.assertIn('value="health"', body)

    def test_a_saved_answer_is_shown_with_its_date(self):
        self.fill()
        body = self.client.get("/health").text
        self.assertIn("Сотрудники без департамента", body)
        self.assertIn("117", body)

    def test_only_a_check_with_rows_is_a_link(self):
        self.fill()
        body = self.client.get("/health").text
        self.assertIn("/health/itmo_no_dept", body)
        self.assertNotIn("/health/pub_no_abstract", body)

    def test_a_clean_check_does_not_repeat_a_zero_as_a_share(self):
        # "0" beside "0.0% из 189" says the same thing twice.
        health.save(self.db, {"checks": [check("clean", n=0, of=189, pct=0.0)]})
        self.assertNotIn("0.0%", self.client.get("/health").text)

    def test_but_a_check_with_nothing_to_measure_says_so(self):
        # Otherwise zero out of zero looks like an honest zero, and that is
        # the difference between "all is well" and "the check checks nothing".
        health.save(self.db, {"checks": [check("empty", n=0, of=0, pct=None)]})
        self.assertIn("не из чего считать", self.client.get("/health").text)

    def test_a_check_that_found_something_keeps_its_share(self):
        health.save(self.db, {"checks": [check("found", status="fail", n=117, of=189, pct=61.9)]})
        self.assertIn("61.9% из 189", self.client.get("/health").text)

    def test_the_page_asks_the_graph_nothing(self):
        # Thirty-odd queries on every open is a page nobody opens twice.
        self.fill()
        with patch.object(deps, "graph_for", side_effect=AssertionError("graph was opened")):
            self.assertEqual(self.client.get("/health").status_code, 200)

    def test_a_viewer_reads_it_but_cannot_recompute(self):
        self.fill()
        guest = self.sign_in("guest")
        body = guest.get("/health").text
        self.assertIn("Сотрудники без департамента", body)
        self.assertNotIn('value="health"', body)

    def test_recomputing_queues_a_job(self):
        self.client.post("/jobs", data={"csrf": self.csrf(self.client), "kind": "health"})
        (row,) = list(self.db[store.COLLECTION].find())
        job = store.read(self.db, row["_id"])
        self.assertEqual(job.kind, JobKind.HEALTH)
        # Reads only, but it still waits for whatever is rewriting the
        # graph: measuring a half-published graph measures nothing.
        self.assertEqual(job.resource, "graph")

    def test_an_unknown_check_is_a_404(self):
        self.assertEqual(self.client.get("/health/never-heard-of-it").status_code, 404)


class RowsBehindACheckTest(unittest.TestCase):
    """The drill-down goes to the graph, and does it now rather than then."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "chief", "hunter2", role="admin")
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: FakePanelGraph()
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "chief", "password": "hunter2"})

    def found(self, **over):
        row = {"id": "itmo_no_dept", "title": "Сотрудники без департамента",
               "title_en": "x", "group": "Пропуски", "group_en": "x",
               "hint": None, "hint_en": None, "total": 2,
               "columns": ["id", "ФИО"], "rows": [["A1", "Иванов"], ["A2", "Петров"]],
               "shown": 2, "limit": 300, "truncated": False}
        row.update(over)
        return row

    def test_the_rows_are_shown(self):
        with patch("pauk.admin.health_routes.health.rows_behind", return_value=self.found()):
            body = self.client.get("/health/itmo_no_dept").text
        self.assertIn("Иванов", body)
        self.assertIn("Петров", body)

    def test_an_empty_result_says_it_may_have_been_fixed(self):
        with patch("pauk.admin.health_routes.health.rows_behind",
                   return_value=self.found(rows=[], shown=0, total=0)):
            body = self.client.get("/health/itmo_no_dept").text
        self.assertIn("Сейчас таких записей нет", body)

    def test_a_capped_list_says_so(self):
        with patch("pauk.admin.health_routes.health.rows_behind",
                   return_value=self.found(total=900, shown=300, truncated=True)):
            body = self.client.get("/health/itmo_no_dept").text
        self.assertIn("Показаны первые 300", body)

    def test_the_csv_carries_the_same_rows(self):
        with patch("pauk.admin.health_routes.health.rows_behind", return_value=self.found()):
            response = self.client.get("/health/itmo_no_dept/csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("itmo_no_dept.csv", response.headers["content-disposition"])
        self.assertIn("Иванов", response.text)

    def test_the_csv_starts_with_a_mark_excel_understands(self):
        # These carry Russian names, and Excel reads a CSV as the system
        # encoding unless the file says otherwise.
        with patch("pauk.admin.health_routes.health.rows_behind", return_value=self.found()):
            response = self.client.get("/health/itmo_no_dept/csv")
        self.assertTrue(response.text.startswith("﻿"))

    def test_a_check_with_no_examples_query_is_refused(self):
        with patch("pauk.admin.health_routes.health.rows_behind",
                   side_effect=ValueError("у проверки нет запроса за примерами")):
            self.assertEqual(self.client.get("/health/itmo_no_dept").status_code, 400)
