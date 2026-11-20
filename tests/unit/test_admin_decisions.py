import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable
from pymongo.errors import AutoReconnect

from pauk.admin import decision_routes, decisions, deps, feed
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user, session_key
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.overrides import (
    active_overrides,
    apply_overrides,
    record_override,
    record_relationship_override,
    relationship_override_id,
    tombstoned_ids,
    tombstoned_relationships,
)
from pauk.jobs.locks import acquire, release
from pauk.jobs.models import GRAPH
from pauk.jobs.worker import Worker
from pauk.settings import Settings
from pauk.storage import review, version_repair
from tests.unit.admin_forms import RenderedForms
from tests.unit.test_admin_nodes import FakePanelGraph


def source_wrote(db, label, node_id, field, before, after, after_hours=1,
                 actor="pipeline", source="publish"):
    """Record a write by the pipeline, placed relative to the decision.

    The time is computed from the decision's own `created_at` rather than
    written as a date: conflicts only count writes that came *after* the
    edit, so a fixed timestamp silently changes meaning as the calendar
    moves - these tests passed on one day and failed on the next.
    """
    from datetime import timedelta
    row = db["graph_overrides"].find_one({"target_id": node_id})
    base = row["created_at"] if row else datetime.now(UTC)
    db[feed.COLLECTION].insert_one({
        "timestamp": (base + timedelta(hours=after_hours)).isoformat(),
        "actor": actor, "source": source, "entity_type": label,
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

    def test_cli_edits_do_not_dispute_their_own_manual_decision(self):
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "Пётр",
                     actor="user:roman", source="admin-cli")
        self.assertEqual(decisions.conflicts(self.db), [])

    def test_a_real_source_change_after_a_cli_edit_is_still_disputed(self):
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "Пётр",
                     actor="user:roman", source="admin-cli")
        source_wrote(self.db, "Person", "A1", "name_ru", "Пётр", "Иван Петров", after_hours=2)
        (conflict,) = decisions.conflicts(self.db)
        self.assertEqual(conflict["now"], "Иван Петров")
        self.assertEqual(conflict["ours"], "Пётр")

    def test_json_formatting_changes_are_not_a_new_source_opinion(self):
        automatic = '[\n  {"grant_id":"42", "funder":"Фонд"}\n]'
        record_override(self.db, "Publication", "W9", "set", {"funding": [{"grant_id": "99"}]},
                        auto_value={"funding": automatic})
        source_wrote(self.db, "Publication", "W9", "funding", automatic,
                     '[{"funder":"Фонд","grant_id":"42"}]')
        self.assertFalse([row for row in decisions.conflicts(self.db) if row["target_id"] == "W9"])

    def test_writes_from_before_the_decision_are_ignored(self):
        # Deliberately a value that differs from auto_value: otherwise the
        # row would be dropped for repeating itself and the test would pass
        # whether or not the time filter works at all.
        source_wrote(self.db, "Person", "A1", "name_ru", "Ivan", "Ivan Petrov",
                     after_hours=-24)
        self.assertEqual(decisions.conflicts(self.db), [])

    def test_the_latest_word_of_the_source_is_the_one_that_counts(self):
        source_wrote(self.db, "Person", "A1", "name_ru", "Иван", "И. Петров", after_hours=1)
        source_wrote(self.db, "Person", "A1", "name_ru", "И. Петров", "И. П. Петров",
                     after_hours=2)
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
        return self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

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
        self.sign_in()
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", target_id="A1")
        response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 303)
        self.assertEqual(active_overrides(self.db), [])

    def test_undoing_a_link_decision_restores_it_at_once(self):
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication", "A1", "W1",
                                     actor="user:roman")
        self.graph.add("Publication", "W1")
        self.sign_in()
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", kind="rel")
        self.assertEqual(self.client.post(form["action"], data=form["fields"]).status_code, 303)
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)
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
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def undo(self, target_id):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", target_id=target_id)
        return self.client.post(form["action"], data=form["fields"])

    def test_failed_node_withdrawal_removes_the_restored_node(self):
        with patch.object(decision_routes, "deactivate_override", side_effect=AutoReconnect("before commit")):
            response = self.undo("D1")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(("Department", "D1"), self.graph.nodes)
        self.assertTrue(self.db["graph_overrides"].find_one({"target_id": "D1"})["active"])

    def test_failed_relationship_withdrawal_removes_the_restored_edge(self):
        with patch.object(decision_routes, "deactivate_relationship_override", side_effect=AutoReconnect("before commit")):
            response = self.undo("W1")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.graph.relationships)
        self.assertTrue(self.db["graph_overrides"].find_one({"kind": "rel"})["active"])

    def test_missing_endpoint_preserves_the_relationship_decision(self):
        del self.graph.nodes["Publication", "W1"]
        self.assertEqual(self.undo("W1").status_code, 400)
        self.assertTrue(self.db["graph_overrides"].find_one({"kind": "rel"})["active"])

    def test_a_stale_relationship_form_keeps_the_new_decision(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", kind="rel")
        record_relationship_override(self.db, "Person", "AUTHORED", "Publication", "A1", "W1",
                                     snapshot={"is_corresponding": True})
        response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.graph.relationships)
        self.assertTrue(self.db["graph_overrides"].find_one({"kind": "rel"})["active"])

    def test_a_recreated_node_is_not_replaced_by_the_old_snapshot(self):
        self.graph.add("Department", "D1", name_ru="Newer data")
        self.assertEqual(self.undo("D1").status_code, 303)
        self.assertEqual(self.graph.nodes["Department", "D1"]["name_ru"], "Newer data")

    def test_a_recreated_edge_is_not_replaced_by_the_old_snapshot(self):
        edge = ("Person", "AUTHORED", "Publication", "A1", "W1")
        self.graph.relationships[edge] = {"is_corresponding": True}
        self.assertEqual(self.undo("W1").status_code, 303)
        self.assertEqual(self.graph.relationships[edge], {"is_corresponding": True})

    def test_a_legacy_relationship_decision_without_a_write_token_can_be_undone(self):
        key = relationship_override_id("Person", "AUTHORED", "Publication", "A1", "W1")
        self.db["graph_overrides"].update_one({"_id": key}, {"$unset": {"write_id": ""}})
        self.assertEqual(self.undo("W1").status_code, 303)

    def test_undoing_a_deleted_node_brings_it_back_with_its_fields(self):
        # Lifting the ban alone leaves the graph unchanged: the record would
        # reappear only at the next publish, and the button would look broken.
        response = self.undo("D1")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.graph.nodes[("Department", "D1")]["name_ru"], "Кафедра")

    def test_undoing_an_unlinked_relationship_links_it_again(self):
        response = self.undo("W1")
        self.assertEqual(response.status_code, 303)
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.graph.relationships)

    def test_the_page_says_what_came_back(self):
        response = self.undo("D1")
        self.assertIn("undone=node", response.headers["location"])

    def test_undoing_a_field_edit_puts_the_source_value_back(self):
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр"},
                        actor="user:roman", auto_value={"name_ru": "Иван"})
        response = self.undo("A1")
        self.assertIn("undone=field", response.headers["location"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иван")

    def test_a_deletion_the_feed_cannot_describe_says_so_instead(self):
        record_override(self.db, "Person", "A9", "delete", actor="user:roman")
        response = self.undo("A9")
        self.assertIn("undone=1", response.headers["location"])
        self.assertNotIn(("Person", "A9"), self.graph.nodes)


class VersionDeletionUndoTest(unittest.TestCase):
    """Removing a version tombstone must not restore yesterday's acceptance."""

    def setUp(self):
        self.db = mongomock.MongoClient().pauk_test
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        for work_id, day in (("W1", "2024-01-01"), ("W2", "2024-06-01")):
            self.graph.add(
                "Publication", work_id, title="Robust neural network classification method",
                doi=f"10.1/{work_id}", publication_date=day,
                authors=[{"person_id": "A1"}],
            )
        self.edge = ("Publication", "IS_VERSION", "Publication", "W2", "W1")
        self.snapshot = {
            "bucket": "MISSED_BY_NORM", "mergeable": True, "title_sim": 1.0,
            "status": "same", "confirmation_source": "auto", "confirmation_policy": "rules-v1",
        }
        record_relationship_override(self.db, *self.edge, actor="user:roman", snapshot=self.snapshot)
        self.document_id = relationship_override_id(*self.edge)
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        app.dependency_overrides[deps.graph_if_up] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def undo(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", kind="rel")
        return self.client.post(form["action"], data=form["fields"])

    def deletion_active(self):
        return self.db.graph_overrides.find_one({"_id": self.document_id})["active"]

    def test_old_auto_confirmation_is_recomputed_and_held_question_is_created(self):
        self.assertEqual(self.undo().status_code, 303)
        self.assertFalse(self.deletion_active())
        edges = self.graph.fetch_version_edges(("W1", "W2"))
        self.assertEqual(len(edges), 1)
        props = next(iter(edges.values()))
        self.assertEqual(props["status"], "held")
        self.assertNotIn("confirmation_source", props)
        self.assertNotIn("confirmation_policy", props)
        question = self.db[review.COLLECTION].find_one({"kind": review.PUB_VERSION})
        self.assertIsNotNone(question)
        self.assertNotIn("verdict", question)

    def test_qualifying_content_is_reconfirmed_under_current_policy(self):
        abstract = (
            "We evaluate convolutional networks for retinal image classification using independently labelled "
            "photographs collected across several clinical centres. Patient level separation prevents leakage "
            "between training and evaluation cohorts. The proposed calibration procedure improves sensitivity "
            "while preserving specificity under camera changes. External validation measures confidence "
            "intervals, subgroup performance, and robustness against illumination artefacts in diabetic screening."
        )
        for work_id in ("W1", "W2"):
            self.graph.nodes["Publication", work_id]["abstract"] = abstract
        self.assertEqual(self.undo().status_code, 303)
        props = next(iter(self.graph.fetch_version_edges(("W1", "W2")).values()))
        self.assertEqual(props["status"], "same")
        self.assertEqual(props["confirmation_source"], "auto")
        self.assertEqual(props["confirmation_policy"], "rules-v3")
        self.assertFalse(self.db[review.COLLECTION].find_one())

    def test_new_human_same_uses_review_provenance(self):
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        self.assertEqual(self.undo().status_code, 303)
        props = next(iter(self.graph.fetch_version_edges(("W1", "W2")).values()))
        self.assertEqual(props["status"], "same")
        self.assertEqual(props["confirmation_source"], "review")
        self.assertNotIn("confirmation_policy", props)
        self.assertFalse(self.deletion_active())

    def test_new_human_different_prevents_resurrection(self):
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.DIFFERENT)
        self.assertEqual(self.undo().status_code, 303)
        self.assertFalse(self.graph.fetch_version_edges(("W1", "W2")))
        self.assertFalse(self.deletion_active())

    def test_disappeared_candidate_is_not_restored_from_snapshot(self):
        self.graph.nodes["Publication", "W2"].update(
            title="Unrelated chemical synthesis under pressure", authors=[{"person_id": "A2"}]
        )
        self.assertEqual(self.undo().status_code, 303)
        self.assertFalse(self.graph.fetch_version_edges(("W1", "W2")))
        self.assertFalse(self.db[review.COLLECTION].find_one({"kind": review.PUB_VERSION}))
        self.assertFalse(self.deletion_active())

    def test_human_same_can_restore_saved_evidence_after_candidate_disappears(self):
        self.graph.nodes["Publication", "W2"]["title"] = "Unrelated chemical synthesis under pressure"
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        self.assertEqual(self.undo().status_code, 303)
        props = next(iter(self.graph.fetch_version_edges(("W1", "W2")).values()))
        self.assertEqual(props["status"], "same")
        self.assertEqual(props["confirmation_source"], "review")
        self.assertNotIn("confirmation_policy", props)

    def test_recreated_edge_is_not_overwritten_with_snapshot_custom_fields(self):
        current_edge = ("Publication", "IS_VERSION", "Publication", "W1", "W2")
        self.graph.relationships[current_edge] = {**self.snapshot, "context": "newer metadata"}
        self.assertEqual(self.undo().status_code, 303)
        self.assertEqual(self.graph.relationships[current_edge]["context"], "newer metadata")
        self.assertEqual(self.graph.relationships[current_edge]["status"], "held")

    def test_another_direction_tombstone_is_not_ignored(self):
        reverse = ("Publication", "IS_VERSION", "Publication", "W1", "W2")
        record_relationship_override(self.db, *reverse)
        row = self.db.graph_overrides.find_one({"_id": self.document_id})
        session = self.db[SESSIONS].find_one()
        response = self.client.post("/overrides/undo", data={
            "csrf": session["csrf"], "kind": "rel", "src_label": "Publication",
            "rel_type": "IS_VERSION", "tgt_label": "Publication", "src_id": "W2",
            "target_id": "W1", "op": "delete", "seen_write": row["write_id"],
        })
        self.assertEqual(response.status_code, 303)
        self.assertFalse(self.graph.fetch_version_edges(("W1", "W2")))
        self.assertTrue(self.db.graph_overrides.find_one({"_id": relationship_override_id(*reverse)})["active"])

    def test_missing_endpoint_preserves_the_tombstone(self):
        del self.graph.nodes["Publication", "W1"]
        self.assertEqual(self.undo().status_code, 400)
        self.assertTrue(self.deletion_active())
        self.assertFalse(self.graph.relationships)

    def test_busy_graph_keeps_both_stores_unchanged(self):
        self.assertTrue(acquire(self.db, GRAPH, "another-writer"))
        try:
            self.assertEqual(self.undo().status_code, 409)
        finally:
            release(self.db, GRAPH, "another-writer")
        self.assertTrue(self.deletion_active())
        self.assertFalse(self.graph.relationships)
        self.assertFalse(self.db[review.COLLECTION].find_one())

    def test_failed_mongo_withdrawal_leaves_graph_and_questions_untouched(self):
        with patch.object(decision_routes, "deactivate_relationship_override", side_effect=AutoReconnect("offline")):
            self.assertEqual(self.undo().status_code, 503)
        self.assertTrue(self.deletion_active())
        self.assertFalse(self.graph.relationships)
        self.assertFalse(self.db[review.COLLECTION].find_one())

    def test_partially_failed_projection_is_retryable_without_snapshot_rollback(self):
        original = self.graph.upsert_relationships_batch
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", kind="rel")

        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise ServiceUnavailable("lost graph reply")

        with patch.object(self.graph, "upsert_relationships_batch", side_effect=fail_after_write):
            response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 303)
        self.assertIn("undone=pending", response.headers["location"])
        self.assertFalse(self.deletion_active())
        source = self.db.graph_overrides.find_one({"_id": self.document_id})
        self.assertIn(version_repair.FIELD, source)
        self.assertEqual(next(iter(self.graph.relationships.values()))["status"], "held")
        retry = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(retry.status_code, 303)
        self.assertNotIn("pending", retry.headers["location"])
        self.assertNotIn(version_repair.FIELD, self.db.graph_overrides.find_one({"_id": self.document_id}))
        self.assertIsNotNone(self.db[review.COLLECTION].find_one({"kind": review.PUB_VERSION}))

    def test_failed_withdrawal_leaves_a_preexisting_edge_untouched(self):
        before = {"bucket": "MISSED_BY_NORM", "status": "held", "context": "current metadata"}
        self.graph.relationships[self.edge] = before.copy()
        with patch.object(decision_routes, "deactivate_relationship_override", side_effect=AutoReconnect("offline")):
            self.assertEqual(self.undo().status_code, 503)
        self.assertEqual(self.graph.relationships[self.edge], before)
        self.assertTrue(self.deletion_active())

    def test_failed_commit_never_runs_destructive_snapshot_compensation(self):
        with (
            patch.object(decision_routes, "deactivate_relationship_override", side_effect=AutoReconnect("offline")),
            patch.object(self.graph, "delete_relationships_batch", side_effect=ServiceUnavailable("must not run")) as delete,
        ):
            self.assertEqual(self.undo().status_code, 503)
            delete.assert_not_called()
        self.assertTrue(self.deletion_active())
        self.assertFalse(self.graph.relationships)

    def test_queue_failure_is_rediscovered_and_completed_by_the_worker(self):
        with patch.object(review, "record_held", side_effect=AutoReconnect("queue unavailable")):
            response = self.undo()
        self.assertEqual(response.status_code, 303)
        self.assertIn("undone=pending", response.headers["location"])
        self.assertFalse(self.deletion_active())
        props = next(iter(self.graph.fetch_version_edges(("W1", "W2")).values()))
        self.assertEqual(props["status"], "held")
        self.assertNotIn("confirmation_source", props)
        self.assertIn(version_repair.FIELD, self.db.graph_overrides.find_one({"_id": self.document_id}))
        self.db.graph_overrides.update_one({"_id": self.document_id}, {
            "$set": {f"{version_repair.FIELD}.next_attempt_at": datetime.min.replace(tzinfo=UTC)},
        })
        with patch("pauk.graph.version_repair.audited_client", return_value=self.graph):
            self.assertTrue(Worker(Settings(), self.db, name="repair-test").run_once())
        self.assertNotIn(version_repair.FIELD, self.db.graph_overrides.find_one({"_id": self.document_id}))
        question = self.db[review.COLLECTION].find_one({"kind": review.PUB_VERSION})
        self.assertIsNotNone(question)
        self.assertNotIn("verdict", question)

    def test_offline_graph_saves_a_pending_undo_without_touching_the_graph(self):
        self.client.app.dependency_overrides[deps.graph_if_up] = lambda: None
        response = self.undo()
        self.assertEqual(response.status_code, 303)
        self.assertIn("undone=pending", response.headers["location"])
        self.assertFalse(self.deletion_active())
        self.assertFalse(self.graph.relationships)
        self.assertIn(version_repair.FIELD, self.db.graph_overrides.find_one({"_id": self.document_id}))

    def test_pending_undo_stays_visible_and_can_be_retried_without_original_form(self):
        self.client.app.dependency_overrides[deps.graph_if_up] = lambda: None
        self.assertEqual(self.undo().status_code, 303)
        page = self.client.get("/overrides")
        self.assertIn("Ожидает согласования версий", page.text)
        retry = RenderedForms(page.text).with_button("повторить согласование")
        self.client.app.dependency_overrides[deps.graph_if_up] = lambda: self.graph
        response = self.client.post(retry["action"], data=retry["fields"])
        self.assertEqual(response.status_code, 303)
        self.assertIn("repair=applied", response.headers["location"])
        self.assertNotIn(version_repair.FIELD, self.db.graph_overrides.find_one({"_id": self.document_id}))
        self.assertIsNotNone(self.db[review.COLLECTION].find_one({"kind": review.PUB_VERSION}))
        stale = self.client.post(retry["action"], data=retry["fields"])
        self.assertEqual(stale.status_code, 409)

    def test_a_new_decision_prevents_retry_of_an_old_undo_form(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", kind="rel")
        self.assertEqual(self.client.post(form["action"], data=form["fields"]).status_code, 303)
        record_relationship_override(self.db, *self.edge)
        response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.deletion_active())


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
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def delete_it(self):
        return self.client.post("/nodes/Department/delete/D1", data={"csrf": self.csrf})

    def test_deleting_stores_the_fields_on_the_decision(self):
        self.delete_it()
        (row,) = active_overrides(self.db)
        self.assertEqual(row["snapshot"]["name_ru"], "Кафедра")
        self.assertEqual(row["snapshot"]["kind"], "chair")

    def test_restoring_works_with_an_empty_feed(self):
        # The feed records history, not state, and summarises a bulk
        # operation without listing fields - restoring must not depend on
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
    """Reapplying records the value it covers up - the source's own word."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_ru="Иван")
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр"},
                        actor="user:roman", auto_value={"name_ru": "Иван"})

    def test_applying_stores_what_the_pipeline_had(self):
        from pauk.graph.overrides import apply_overrides
        self.graph.nodes[("Person", "A1")]["name_ru"] = "И. П. Петров"   # the source's word
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
        self.graph.nodes[("Person", "A1")]["name_ru"] = "Иван"    # unchanged
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
        # The link is checked, not the label: button text gets rewritten.
        self.assertIn("/overrides?tab=list&page=1", body)

    def test_a_page_out_of_range_does_not_break(self):
        self.assertEqual(self.client.get("/overrides", params={"page": 99}).status_code, 200)
        self.assertEqual(self.client.get("/overrides", params={"page": 0}).status_code, 200)

    def test_conflicts_are_paged_too(self):
        # They are computed rather than stored, so paging happens after the
        # comparisons - but the page still has to be bounded.
        for n in range(decisions.PAGE + 5):
            record_override(self.db, "Person", f"B{n:03}", "set", {"name_ru": "Пётр"},
                            actor="user:roman", auto_value={"name_ru": "Иван"})
            self.db["graph_overrides"].update_one(
                {"_id": f"node:Person:B{n:03}"},
                {"$set": {"source_value": {"name_ru": "И. П. Петров"}}})
        self.assertEqual(len(decisions.conflicts(self.db)), decisions.PAGE)
        self.assertEqual(decisions.count_conflicts(self.db), decisions.PAGE + 5)


class UndoRestoresFieldTest(unittest.TestCase):
    """Withdrawing an edit has to put the field back, not just stop applying it."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_ru="Пётр Иванов")
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Пётр Иванов"},
                        actor="user:roman", auto_value={"name_ru": "Иван Петров"})

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def undo(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить")
        return self.client.post(form["action"], data=form["fields"])

    def test_a_graph_failure_keeps_the_manual_decision_active(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить")
        with patch.object(self.graph, "update_node_properties", side_effect=ServiceUnavailable("offline")):
            response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 503)
        self.assertTrue(self.db["graph_overrides"].find_one({"target_id": "A1"})["active"])
        self.assertEqual(self.graph.nodes["Person", "A1"]["name_ru"], "Пётр Иванов")

    def test_a_stale_form_cannot_undo_a_newer_edit(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить")
        self.graph.nodes["Person", "A1"]["name_ru"] = "New editor"
        record_override(self.db, "Person", "A1", "set", {"name_ru": "New editor"}, actor="other")
        response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.db["graph_overrides"].find_one({"target_id": "A1"})["active"])
        self.assertEqual(self.graph.nodes["Person", "A1"]["name_ru"], "New editor")

    def test_failed_withdrawal_restores_the_graph_value(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить")
        with patch.object(decision_routes, "deactivate_override", side_effect=AutoReconnect("before commit")):
            response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.graph.nodes["Person", "A1"]["name_ru"], "Пётр Иванов")
        self.assertTrue(self.db["graph_overrides"].find_one({"target_id": "A1"})["active"])

    def test_an_old_form_without_a_version_requests_a_reload(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить")
        del form["fields"]["seen_write"]
        response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.db["graph_overrides"].find_one({"target_id": "A1"})["active"])

    def test_an_orphaned_field_decision_can_be_withdrawn_without_recreating_the_node(self):
        del self.graph.nodes["Person", "A1"]
        self.assertEqual(self.undo().status_code, 303)
        self.assertNotIn(("Person", "A1"), self.graph.nodes)
        self.assertFalse(self.db["graph_overrides"].find_one({"target_id": "A1"})["active"])

    def test_withdrawing_one_decision_does_not_reapply_unrelated_decisions(self):
        self.graph.add("Person", "A2", name_ru="Source")
        record_override(self.db, "Person", "A2", "set", {"name_ru": "Other manual"})
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить", target_id="A1")
        self.assertEqual(self.client.post(form["action"], data=form["fields"]).status_code, 303)
        self.assertEqual(self.graph.nodes["Person", "A2"]["name_ru"], "Source")

    def test_compensation_cannot_overwrite_a_concurrent_edit(self):
        original = decision_routes.deactivate_override

        def edit_before_withdrawal(*args, **kwargs):
            self.graph.add("Person", "A1", name_ru="Concurrent edit")
            record_override(self.db, "Person", "A1", "set", {"name_ru": "Concurrent edit"}, actor="other")
            return original(*args, **kwargs)

        with patch.object(decision_routes, "deactivate_override", edit_before_withdrawal):
            response = self.undo()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.graph.nodes["Person", "A1"]["name_ru"], "Concurrent edit")
        row = self.db["graph_overrides"].find_one({"target_id": "A1"})
        self.assertTrue(row["active"])
        self.assertEqual(row["fields"]["name_ru"], "Concurrent edit")

    def test_a_lost_withdrawal_reply_can_confirm_the_commit(self):
        collection = self.db["graph_overrides"]
        original = collection.find_one_and_update

        def commit_without_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise AutoReconnect("reply lost")

        with patch.object(collection, "find_one_and_update", commit_without_reply):
            self.assertEqual(self.undo().status_code, 303)
        self.assertFalse(collection.find_one({"target_id": "A1"})["active"])
        self.assertEqual(self.graph.nodes["Person", "A1"]["name_ru"], "Иван Петров")

    def test_an_unconfirmed_withdrawal_does_not_roll_back_the_graph(self):
        collection = self.db["graph_overrides"]
        original_write, original_read = collection.find_one_and_update, collection.find_one
        written = False
        form = RenderedForms(self.client.get("/overrides").text).with_button("отменить")

        def lose_reply(*args, **kwargs):
            nonlocal written
            original_write(*args, **kwargs)
            written = True
            raise AutoReconnect("reply lost")

        def readback(*args, **kwargs):
            if written:
                raise AutoReconnect("cannot verify")
            return original_read(*args, **kwargs)

        with (patch.object(collection, "find_one_and_update", lose_reply),
              patch.object(collection, "find_one", readback)):
            response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 503)
        self.assertIn("не подтвердила", response.json()["detail"])
        self.assertEqual(self.graph.nodes["Person", "A1"]["name_ru"], "Иван Петров")

    def test_a_driver_failure_during_compensation_is_reported_as_incomplete(self):
        original = self.graph.update_node_properties
        writes = 0

        def update(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise ServiceUnavailable("graph went offline")
            return original(*args, **kwargs)

        with (patch.object(self.graph, "update_node_properties", update),
              patch.object(decision_routes, "deactivate_override", side_effect=AutoReconnect("before commit"))):
            response = self.undo()
        self.assertEqual(response.status_code, 503)
        self.assertIn("не откатилась", response.json()["detail"])
        self.assertTrue(self.db["graph_overrides"].find_one({"target_id": "A1"})["active"])

    def test_a_legacy_node_decision_without_a_write_token_can_be_undone(self):
        self.db["graph_overrides"].update_one({"target_id": "A1"}, {"$unset": {"write_id": ""}})
        self.assertEqual(self.undo().status_code, 303)

    def test_the_field_goes_back_at_once(self):
        # Waiting for a publish is not enough: apply_overrides applies what
        # is in force, and a withdrawn decision instructs nothing. The hand
        # value would sit in the graph until a run happened to touch that
        # field - and for a record the pipeline no longer covers, forever.
        self.undo()
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иван Петров")

    def test_the_page_says_the_field_came_back(self):
        self.assertIn("undone=field", self.undo().headers["location"])

    def test_the_latest_word_of_the_source_wins_over_the_first(self):
        # source_value is written when a publish is covered up, so it is
        # fresher than auto_value from the original edit.
        self.db["graph_overrides"].update_one(
            {"_id": "node:Person:A1"}, {"$set": {"source_value": {"name_ru": "И. П. Петров"}}})
        self.undo()
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "И. П. Петров")

    def test_a_decision_without_a_recorded_source_leaves_the_field_alone(self):
        self.db["graph_overrides"].update_one(
            {"_id": "node:Person:A1"}, {"$unset": {"auto_value": "", "source_value": ""}})
        response = self.undo()
        self.assertIn("undone=1", response.headers["location"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Пётр Иванов")


class PartiallyRewrittenTest(unittest.TestCase):
    """One decision, one field the pipeline fills and one it does not.

    E.g. a fixed name plus an ORCID that OpenAlex never had. Publishing rewrites the first field and
    leaves the second alone, so the graph holds the source's value for one and this override's own value
    for the other.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_en="SRC-A", name_ru="SRC-B")
        self.graph.nodes[("Person", "A1")].update({"name_en": "MAN-A", "name_ru": "MAN-B"})
        record_override(self.db, "Person", "A1", "set",
                        {"name_en": "MAN-A", "name_ru": "MAN-B"}, actor="user:roman",
                        auto_value={"name_en": "SRC-A", "name_ru": "SRC-B"})
        # the publish rewrote only one field
        self.graph.nodes[("Person", "A1")]["name_en"] = "SRC-A2"
        from pauk.graph.overrides import apply_overrides
        apply_overrides(self.graph, self.db)
        self.row = self.db["graph_overrides"].find_one({"_id": "node:Person:A1"})

    def test_only_what_the_source_rewrote_is_recorded_as_its_word(self):
        # The untouched field still holds this override's own value, and
        # storing that as the source's word would be a lie about who said
        # it.
        self.assertEqual(self.row["source_value"], {"name_en": "SRC-A2"})

    def test_no_phantom_conflict_on_the_untouched_field(self):
        self.assertEqual([row["field"] for row in decisions.conflicts(self.db)], ["name_en"])

    def test_undoing_restores_the_pre_edit_value_of_the_untouched_field(self):
        # The failure this guards against is quiet: source_value wins over
        # auto_value, so a field wrongly recorded there would be "restored"
        # to the very value the undo was meant to remove.
        back = decisions.source_of_truth(self.db, "Person", "A1")
        self.assertEqual(back["name_ru"], "SRC-B")
        self.assertEqual(back["name_en"], "SRC-A2")


class UndoingADeletionKeepsTheEditTest(unittest.TestCase):
    """Correcting a field, deleting the record, then taking the deletion back.

    One document holds both decisions. Switching it off whole would bring the record back with the
    corrected value but drop the correction, so the next publish would overwrite it.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_ru="Иванов И.", name_en="I. Ivanov")
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]
        self.client.post("/nodes/Person/A1", data={
            "csrf": self.csrf, "name_ru": "Иванов Иван Петрович", "name_en": "I. Ivanov",
            "seen_at": self.graph.nodes[("Person", "A1")]["updated_at"]})
        self.client.post("/nodes/Person/delete/A1", data={"csrf": self.csrf})

    def take_it_back(self, how):
        if how == "undo":
            form = RenderedForms(self.client.get("/overrides").text).with_button("отменить")
            return self.client.post(form["action"], data=form["fields"])
        form = RenderedForms(self.client.get("/nodes/Person/A1").text).with_button("Восстановить")
        return self.client.post(form["action"], data=form["fields"])

    def publish(self):
        load_prepared_rows(
            self.graph,
            {"persons.jsonl": [{"id": "A1", "is_itmo": True, "name_ru": "Иванов И."}]},
            tombstoned_relationships(self.db), tombstoned_ids(self.db, "LinkCandidate"))
        apply_overrides(self.graph, self.db)

    def test_undo_brings_the_record_back_at_once(self):
        self.take_it_back("undo")
        self.assertIn(("Person", "A1"), self.graph.nodes)

    def test_restore_brings_the_record_back_at_once(self):
        self.take_it_back("restore")
        self.assertIn(("Person", "A1"), self.graph.nodes)

    def test_the_edit_outlives_the_next_publish_after_undo(self):
        self.take_it_back("undo")
        self.publish()
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иванов Иван Петрович")

    def test_the_edit_outlives_the_next_publish_after_restore(self):
        self.take_it_back("restore")
        self.publish()
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иванов Иван Петрович")

    def test_the_decision_still_reads_as_an_edit(self):
        self.take_it_back("undo")
        (row,) = active_overrides(self.db)
        self.assertEqual(row["op"], "set")

    def test_the_record_is_no_longer_tombstoned(self):
        self.take_it_back("undo")
        self.assertEqual(tombstoned_ids(self.db, "Person"), set())
