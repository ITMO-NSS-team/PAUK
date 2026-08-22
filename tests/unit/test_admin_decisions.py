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


class UndoRestoresTest(unittest.TestCase):
    """Undoing a deletion has to put the record back, not only lift the ban."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1"}
        self.graph.nodes[("Publication", "W1")] = {"id": "W1"}

        record_override(self.db, "Department", "D1", "delete", actor="user:roman")
        self.db[feed.COLLECTION].insert_one({
            "timestamp": "2026-08-22T10:00:00", "actor": "user:roman", "source": "admin-ui",
            "entity_type": "Department", "entity_id": "D1", "change_kind": "deleted",
            "diff": {"id": ["D1", None], "name_ru": ["Кафедра", None]}})
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication",
                                     "A1", "W1", actor="user:roman")

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

    def test_undoing_a_deleted_node_brings_it_back_with_its_fields(self):
        # Lifting the ban alone left the graph unchanged: the record would
        # reappear only at the next publish, and the button looked broken.
        response = self.client.post("/overrides/undo", data={
            "csrf": self.csrf, "kind": "node", "op": "delete",
            "label": "Department", "target_id": "D1"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.graph.nodes[("Department", "D1")]["name_ru"], "Кафедра")

    def test_undoing_an_unlinked_relationship_links_it_again(self):
        response = self.client.post("/overrides/undo", data={
            "csrf": self.csrf, "kind": "rel", "op": "delete", "src_label": "Person",
            "rel_type": "AUTHORED", "tgt_label": "Publication",
            "src_id": "A1", "target_id": "W1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.graph.relationships)

    def test_the_page_says_what_came_back(self):
        response = self.client.post("/overrides/undo", data={
            "csrf": self.csrf, "kind": "node", "op": "delete",
            "label": "Department", "target_id": "D1"})
        self.assertIn("undone=node", response.headers["location"])

    def test_undoing_a_field_edit_restores_nothing_by_hand(self):
        # There the point is the opposite: let the pipeline's value show
        # through again, which reapplying the rest already does.
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр"},
                        actor="user:roman", auto_value={"name_ru": "Иван"})
        response = self.client.post("/overrides/undo", data={
            "csrf": self.csrf, "kind": "node", "op": "set",
            "label": "Person", "target_id": "A1"})
        self.assertIn("undone=1", response.headers["location"])

    def test_a_deletion_the_feed_cannot_describe_says_so_instead(self):
        record_override(self.db, "Person", "A9", "delete", actor="user:roman")
        response = self.client.post("/overrides/undo", data={
            "csrf": self.csrf, "kind": "node", "op": "delete",
            "label": "Person", "target_id": "A9"})
        self.assertIn("undone=1", response.headers["location"])
        self.assertNotIn(("Person", "A9"), self.graph.nodes)


class SnapshotTest(unittest.TestCase):
    """A decision carries what it removed, so restoring needs no feed."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Department", "D1", name_ru="Кафедра", kind="chair")

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

    def delete_it(self):
        return self.client.post("/nodes/Department/D1/delete", data={"csrf": self.csrf})

    def test_deleting_stores_the_fields_on_the_decision(self):
        self.delete_it()
        (row,) = active_overrides(self.db)
        self.assertEqual(row["snapshot"]["name_ru"], "Кафедра")
        self.assertEqual(row["snapshot"]["kind"], "chair")

    def test_restoring_works_with_an_empty_feed(self):
        # The feed records history, not state, and summarises a bulk
        # operation without listing fields — restoring must not depend on
        # finding a per-field entry there.
        self.delete_it()
        self.db[feed.COLLECTION].delete_many({})
        self.assertEqual(decisions.deleted_fields(self.db, "Department", "D1")["name_ru"],
                         "Кафедра")

    def test_the_feed_still_answers_for_records_deleted_before_snapshots(self):
        source_wrote(self.db, "Person", "A9", "name_ru", "Иван", None)
        self.db[feed.COLLECTION].update_one(
            {"entity_id": "A9"}, {"$set": {"change_kind": "deleted"}})
        self.assertEqual(decisions.deleted_fields(self.db, "Person", "A9"),
                         {"name_ru": "Иван"})

    def test_reserved_fields_are_not_kept_in_the_snapshot(self):
        # updated_at belongs to the graph, not to the record's content.
        self.delete_it()
        (row,) = active_overrides(self.db)
        self.assertNotIn("updated_at", row["snapshot"])


class SourceValueTest(unittest.TestCase):
    """Reapplying records the value it covers up — the source's own word."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_ru="Иван")
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр"},
                        actor="user:roman", auto_value={"name_ru": "Иван"})

    def test_applying_stores_what_the_pipeline_had(self):
        from pauk.graph.overrides import apply_overrides
        self.graph.nodes[("Person", "A1")]["name_ru"] = "И. П. Петров"   # так сказал источник
        apply_overrides(self.graph, self.db)
        (row,) = active_overrides(self.db)
        self.assertEqual(row["source_value"]["name_ru"], "И. П. Петров")

    def test_the_conflict_is_seen_without_reading_the_feed(self):
        from pauk.graph.overrides import apply_overrides
        self.graph.nodes[("Person", "A1")]["name_ru"] = "И. П. Петров"
        apply_overrides(self.graph, self.db)
        self.db[feed.COLLECTION].delete_many({})
        (conflict,) = decisions.conflicts(self.db)
        self.assertEqual(conflict["was"], "Иван")
        self.assertEqual(conflict["now"], "И. П. Петров")

    def test_a_source_that_agrees_is_not_a_conflict(self):
        from pauk.graph.overrides import apply_overrides
        self.graph.nodes[("Person", "A1")]["name_ru"] = "Иван"    # то же, что и было
        apply_overrides(self.graph, self.db)
        self.assertEqual(decisions.conflicts(self.db), [])


class PagingTest(unittest.TestCase):
    """The list of decisions grows with every edit nobody withdraws."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        for n in range(decisions.PAGE + 20):
            record_override(self.db, "Person", f"A{n:03}", "delete", actor="user:roman")
        self.client = TestClient(build(Settings(neo4j_password=""), self.db),
                                 follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def test_a_page_is_bounded(self):
        self.assertEqual(len(decisions.in_force(self.db)), decisions.PAGE)

    def test_pages_neither_repeat_nor_skip(self):
        first = [row["target_id"] for row in decisions.in_force(self.db)]
        second = [row["target_id"] for row in decisions.in_force(self.db, skip=decisions.PAGE)]
        self.assertEqual(len(first) + len(second), decisions.count_in_force(self.db))
        self.assertFalse(set(first) & set(second))

    def test_the_page_offers_a_way_onward(self):
        body = self.client.get("/overrides").text
        self.assertIn("page=2", body)
        self.assertIn("страница 1 из 2", " ".join(body.split()))

    def test_the_second_page_shows_the_rest(self):
        body = self.client.get("/overrides", params={"page": 2}).text
        self.assertIn("страница 2 из 2", " ".join(body.split()))
        self.assertIn("← новее", body)

    def test_a_page_out_of_range_does_not_break(self):
        self.assertEqual(self.client.get("/overrides", params={"page": 99}).status_code, 200)
        self.assertEqual(self.client.get("/overrides", params={"page": 0}).status_code, 200)

    def test_conflicts_are_paged_too(self):
        # They are computed rather than stored, so paging happens after the
        # comparisons — but the page still has to be bounded.
        for n in range(decisions.PAGE + 5):
            record_override(self.db, "Person", f"B{n:03}", "set", {"name_ru": "Пётр"},
                            actor="user:roman", auto_value={"name_ru": "Иван"})
            self.db["graph_overrides"].update_one(
                {"_id": f"node:Person:B{n:03}"},
                {"$set": {"source_value": {"name_ru": "И. П. Петров"}}})
        self.assertEqual(len(decisions.conflicts(self.db)), decisions.PAGE)
        self.assertEqual(decisions.count_conflicts(self.db), decisions.PAGE + 5)
