import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import decisions, feed
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user
from pauk.graph.overrides import (
    active_overrides,
    record_override,
    record_relationship_override,
)
from pauk.settings import Settings

from .test_admin_nodes import FakePanelGraph


def source_wrote(db, label, node_id, field, before, after, when="2026-08-25T10:00:00",
                 actor="pipeline", source="publish"):
    db[feed.COLLECTION].insert_one({
        "timestamp": when, "actor": actor, "source": source, "entity_type": label,
        "entity_id": node_id, "change_kind": "updated", "diff": {field: [before, after]}})


class InForceTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр"},
                        actor="user:roman", note="по письму", auto_value={"name_ru": "Иван"})
        record_override(self.db, "Publication", "W1", "delete", actor="user:roman", note="дубль")
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication", "A1", "W1",
                                     actor="user:guest", note="матчер ошибся")

    def test_every_kind_of_decision_is_listed(self):
        titles = {row["title"] for row in decisions.in_force(self.db)}
        self.assertIn("Person A1", titles)
        self.assertIn("Publication W1", titles)
        self.assertIn("(Person A1)-[:AUTHORED]->(Publication W1)", titles)

    def test_each_decision_says_what_was_decided(self):
        by_title = {row["title"]: row["what"] for row in decisions.in_force(self.db)}
        self.assertEqual(by_title["Person A1"], "поля изменены")
        self.assertEqual(by_title["Publication W1"], "запись удалена")
        self.assertEqual(by_title["(Person A1)-[:AUTHORED]->(Publication W1)"], "связь удалена")

    def test_an_edited_field_shows_what_it_replaced(self):
        (row,) = [r for r in decisions.in_force(self.db) if r["title"] == "Person A1"]
        self.assertEqual(row["pairs"], [("name_ru", "Иван", "Пётр")])

    def test_a_withdrawn_decision_leaves_the_list(self):
        from pauk.graph.overrides import deactivate_override
        deactivate_override(self.db, "Publication", "W1")
        self.assertNotIn("Publication W1", {r["title"] for r in decisions.in_force(self.db)})


class ConflictTest(unittest.TestCase):
    """A conflict is the source moving, not the graph disagreeing."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр"},
                        actor="user:roman", auto_value={"name_ru": "Иван"})

    def test_a_source_that_changed_its_mind_is_a_conflict(self):
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "И. П. Петров")
        (row,) = decisions.conflicts(self.db)
        self.assertEqual(row["field"], "name_ru")
        self.assertEqual(row["ours"], "Пётр")
        self.assertEqual(row["was"], "Иван")
        self.assertEqual(row["now"], "И. П. Петров")

    def test_a_source_repeating_itself_is_not_a_conflict(self):
        # The pipeline rewriting the same value it had before says nothing
        # new; only a different value means it changed its mind.
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "Иван")
        self.assertEqual(decisions.conflicts(self.db), [])

    def test_the_panel_own_writes_are_not_the_source(self):
        # Reapplying the override writes the field on every publish. Taking
        # that as the source would make every decision look disputed.
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "Пётр",
                     actor="user:roman", source="admin-ui")
        self.assertEqual(decisions.conflicts(self.db), [])

    def test_writes_from_before_the_decision_are_ignored(self):
        # Deliberately a value that differs from auto_value: otherwise the
        # row would be dropped for repeating itself and the test would pass
        # whether or not the time filter works at all.
        source_wrote(self.db, "Person", "A1", "name_ru", "Ivan", "Ivan Petrov",
                     when="2020-01-01T00:00:00")
        self.assertEqual(decisions.conflicts(self.db), [])

    def test_the_latest_word_of_the_source_is_the_one_that_counts(self):
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "И. Петров",
                     when="2026-08-25T10:00:00")
        source_wrote(self.db, "Person", "A1", "name_ru", "И. Петров", "И. П. Петров",
                     when="2026-08-26T10:00:00")
        (row,) = decisions.conflicts(self.db)
        self.assertEqual(row["now"], "И. П. Петров")

    def test_a_deletion_has_no_fields_to_disagree_about(self):
        record_override(self.db, "Publication", "W1", "delete", actor="user:roman")
        source_wrote(self.db, "Publication", "W1", "title", "A", "B")
        self.assertEqual([row["label"] for row in decisions.conflicts(self.db)], [])

    def test_an_untouched_field_is_not_reported(self):
        source_wrote(self.db, "Person", "A1", "orcid", None, "0000-0002")
        self.assertEqual(decisions.conflicts(self.db), [])


class DecisionsPageTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр"},
                        actor="user:roman", note="по письму", auto_value={"name_ru": "Иван"})
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "И. П. Петров")
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1", "name_ru": "Пётр"}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})
        return self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

    def test_the_page_is_closed_without_a_session(self):
        self.assertEqual(self.client.get("/overrides").status_code, 401)

    def test_the_list_shows_the_decision_and_who_made_it(self):
        self.sign_in()
        body = self.client.get("/overrides").text
        self.assertIn("Person A1", body)
        self.assertIn("user:roman", body)
        self.assertIn("по письму", body)

    def test_the_conflicts_tab_shows_the_disagreement(self):
        self.sign_in()
        body = self.client.get("/overrides", params={"tab": "conflicts"}).text
        self.assertIn("И. П. Петров", body)
        self.assertIn("name_ru", body)

    def test_the_tab_carries_the_number_of_conflicts(self):
        self.sign_in()
        self.assertIn("Расхождения 1", " ".join(self.client.get("/overrides").text.split()))

    def test_undoing_stops_applying_the_decision(self):
        csrf = self.sign_in()
        response = self.client.post("/overrides/undo", data={
            "csrf": csrf, "kind": "node", "label": "Person", "target_id": "A1"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(active_overrides(self.db), [])

    def test_undoing_a_link_decision_restores_it_on_the_next_publish(self):
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication", "A1", "W1",
                                     actor="user:roman")
        csrf = self.sign_in()
        self.client.post("/overrides/undo", data={
            "csrf": csrf, "kind": "rel", "src_label": "Person", "rel_type": "AUTHORED",
            "tgt_label": "Publication", "src_id": "A1", "target_id": "W1"})
        self.assertNotIn("rel", {row.get("kind") for row in active_overrides(self.db)})

    def test_a_viewer_sees_the_list_but_no_undo(self):
        self.sign_in(login="guest")
        body = self.client.get("/overrides").text
        self.assertIn("Person A1", body)
        self.assertNotIn("/overrides/undo", body)

    def test_a_viewer_is_refused_at_the_route_too(self):
        csrf = self.sign_in(login="guest")
        response = self.client.post("/overrides/undo", data={
            "csrf": csrf, "kind": "node", "label": "Person", "target_id": "A1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(active_overrides(self.db)), 1)

    def test_undoing_without_the_csrf_token_is_refused(self):
        self.sign_in()
        response = self.client.post("/overrides/undo", data={
            "kind": "node", "label": "Person", "target_id": "A1"})
        self.assertEqual(response.status_code, 403)

    def test_undoing_something_that_is_not_there_is_404(self):
        csrf = self.sign_in()
        response = self.client.post("/overrides/undo", data={
            "csrf": csrf, "kind": "node", "label": "Person", "target_id": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_an_empty_list_says_the_graph_is_all_pipeline(self):
        self.db["graph_overrides"].delete_many({})
        self.sign_in()
        self.assertIn("Ручных правок нет", self.client.get("/overrides").text)

    def test_the_header_carries_the_section(self):
        self.sign_in()
        header = self.client.get("/").text.split("<header>")[1].split("</header>")[0]
        self.assertIn('href="/overrides"', header)


if __name__ == "__main__":
    unittest.main()
