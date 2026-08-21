import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user
from pauk.graph.overrides import COLLECTION, active_overrides
from pauk.settings import Settings

from .test_mutations import FakeGraph


class FakePanelGraph(FakeGraph):
    """FakeGraph plus the two reads the node screens need."""

    def search_nodes(self, label, fields, query, limit=50):
        needle = query.lower()
        found = []
        for (node_label, node_id), props in self.nodes.items():
            if node_label != label:
                continue
            hit = node_id == query or any(
                needle in str(props.get(name, "")).lower() for name in fields)
            if hit:
                row = {"id": node_id, "exact": node_id == query}
                row.update({name: props.get(name) for name in fields})
                found.append(row)
        found.sort(key=lambda row: (not row["exact"], row["id"]))
        return found[:limit]

    def fetch_node_relationships(self, label, node_id):
        found = []
        for src_label, rel_type, tgt_label, src_id, tgt_id in self.relationships:
            if src_label == label and src_id == node_id:
                found.append({"type": rel_type, "labels": [tgt_label],
                              "other_id": tgt_id, "outgoing": True})
            elif tgt_label == label and tgt_id == node_id:
                found.append({"type": rel_type, "labels": [src_label],
                              "other_id": src_id, "outgoing": False})
        return sorted(found, key=lambda row: (row["type"], row["other_id"]))


class NodeScreenTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {
            "id": "A1", "name_en": "Ivan Petrov", "name_ru": "Иван Петров",
            "orcid": "0000-0002-1825-0097", "updated_at": "2026-08-01"}
        self.graph.nodes[("Person", "A2")] = {"id": "A2", "name_en": "Anna Petrova"}

        app = build(Settings(), self.db)
        # The route opens an audited client per request; hand it the fake
        # instead, so the screens are exercised without a live Neo4j.
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})
        token = self.client.cookies[COOKIE]
        return self.db[SESSIONS].find_one({"_id": token})["csrf"]

    def test_the_screens_are_closed_without_a_session(self):
        self.assertEqual(self.client.get("/nodes/Person").status_code, 401)
        self.assertEqual(self.client.get("/nodes/Person/A1").status_code, 401)

    def test_an_unknown_label_is_404_and_never_reaches_cypher(self):
        self.sign_in()
        self.assertEqual(self.client.get("/nodes/Malicious").status_code, 404)
        self.assertEqual(self.client.get("/nodes/Person%20MATCH/A1").status_code, 404)

    def test_search_finds_by_name_and_by_id(self):
        self.sign_in()
        by_name = self.client.get("/nodes/Person", params={"q": "petrov"}).text
        self.assertIn("A1", by_name)
        self.assertIn("A2", by_name)
        by_id = self.client.get("/nodes/Person", params={"q": "A1"}).text
        self.assertIn("A1", by_id)

    def test_an_empty_query_searches_nothing(self):
        self.sign_in()
        self.assertNotIn("A1", self.client.get("/nodes/Person").text)

    def test_a_node_page_shows_its_fields(self):
        self.sign_in()
        body = self.client.get("/nodes/Person/A1").text
        self.assertIn("Ivan Petrov", body)
        self.assertIn("0000-0002-1825-0097", body)

    def test_a_missing_node_is_404(self):
        self.sign_in()
        self.assertEqual(self.client.get("/nodes/Person/nope").status_code, 404)

    def test_an_edit_reaches_the_graph_and_is_recorded_as_a_decision(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A1",
                                    data={"csrf": csrf, "name_ru": "Пётр Иванов", "note": "по письму"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Пётр Иванов")
        (override,) = active_overrides(self.db)
        self.assertEqual(override["fields"], {"name_ru": "Пётр Иванов"})
        self.assertEqual(override["actor"], "user:roman")
        self.assertEqual(override["note"], "по письму")
        # What the pipeline had is kept, so the conflict screen can later
        # say what the source now claims.
        self.assertEqual(override["auto_value"], {"name_ru": "Иван Петров"})

    def test_submitting_the_form_unchanged_records_nothing(self):
        csrf = self.sign_in()
        self.client.post("/nodes/Person/A1", data={"csrf": csrf, "name_en": "Ivan Petrov"})
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_only_the_changed_field_is_written(self):
        csrf = self.sign_in()
        self.client.post("/nodes/Person/A1",
                         data={"csrf": csrf, "name_en": "Ivan Petrov", "name_ru": "Пётр Иванов"})
        (override,) = active_overrides(self.db)
        self.assertEqual(set(override["fields"]), {"name_ru"})

    def test_clearing_a_box_stores_nothing_rather_than_an_empty_string(self):
        csrf = self.sign_in()
        self.client.post("/nodes/Person/A1", data={"csrf": csrf, "orcid": ""})
        self.assertIsNone(self.graph.nodes[("Person", "A1")]["orcid"])

    def test_a_field_outside_the_whitelist_is_dropped_and_the_rest_still_saves(self):
        # The route filters by the whitelist before the mutation layer sees
        # the patch. Without that filter the extra field would reach
        # validate_fields, be refused, and take the legitimate edit down
        # with it — so asserting the junk is absent is not enough on its
        # own; the real edit has to have gone through.
        csrf = self.sign_in()
        response = self.client.post(
            "/nodes/Person/A1",
            data={"csrf": csrf, "name_ru": "Пётр", "is_admin": "yes", "id": "hacked"})
        self.assertEqual(response.status_code, 303)
        node = self.graph.nodes[("Person", "A1")]
        self.assertEqual(node["name_ru"], "Пётр")
        self.assertNotIn("is_admin", node)
        self.assertEqual(node["id"], "A1")

    def test_an_edit_without_the_csrf_token_is_refused(self):
        self.sign_in()
        response = self.client.post("/nodes/Person/A1", data={"name_ru": "Взлом"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иван Петров")

    def test_an_edit_with_a_forged_csrf_token_is_refused(self):
        self.sign_in()
        response = self.client.post("/nodes/Person/A1", data={"csrf": "forged", "name_ru": "Взлом"})
        self.assertEqual(response.status_code, 403)

    def test_a_viewer_cannot_edit(self):
        csrf = self.sign_in(login="guest")
        response = self.client.post("/nodes/Person/A1", data={"csrf": csrf, "name_ru": "Правка"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иван Петров")

    def test_a_viewer_can_still_read(self):
        self.sign_in(login="guest")
        self.assertEqual(self.client.get("/nodes/Person/A1").status_code, 200)

    def test_deleting_removes_the_node_and_leaves_a_tombstone(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A2/delete", data={"csrf": csrf})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Person", "A2"), self.graph.nodes)
        (override,) = active_overrides(self.db)
        self.assertEqual(override["op"], "delete")

    def test_a_refused_delete_leaves_no_tombstone_behind(self):
        # A node with relationships is refused without cascade; a tombstone
        # recorded anyway would delete it on the next publish.
        csrf = self.sign_in()
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")] = {}
        response = self.client.post("/nodes/Person/A1/delete", data={"csrf": csrf})
        self.assertEqual(response.status_code, 400)
        self.assertIn(("Person", "A1"), self.graph.nodes)
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)


if __name__ == "__main__":
    unittest.main()


class RelationshipScreenTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1", "name_en": "Ivan Petrov"}
        self.graph.nodes[("Publication", "W1")] = {"id": "W1", "title": "A paper"}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})
        return self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

    def link_data(self, csrf, **extra):
        return {"csrf": csrf, "triple": "Person|AUTHORED|Publication",
                "other_id": "W1", **extra}

    def test_a_link_is_created_between_two_nodes(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A1/rel/add", data=self.link_data(csrf))
        self.assertEqual(response.status_code, 303)
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)

    def test_a_created_link_needs_no_override_to_survive_publishing(self):
        # The loader never removes edges it does not know about, so there
        # is nothing for a decision to reapply.
        csrf = self.sign_in()
        self.client.post("/nodes/Person/A1/rel/add", data=self.link_data(csrf))
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_a_relationship_outside_the_eleven_known_triples_is_refused(self):
        # Refused by the route, before the graph is touched at all. The
        # mutation layer would refuse it too, but only after a round trip —
        # and asserting on the outcome alone cannot tell the two apart, so
        # the check is that nothing reached the client.
        csrf = self.sign_in()
        for triple in ("Person|OWNS|Publication", "Person|AUTHORED|Malicious",
                       "nonsense", "Person|AUTHORED"):
            self.graph.calls.clear()
            response = self.client.post(
                "/nodes/Person/A1/rel/add",
                data={"csrf": csrf, "triple": triple, "other_id": "W1"})
            self.assertEqual(response.status_code, 400, triple)
            self.assertEqual(self.graph.calls, [], triple)
        self.assertEqual(self.graph.relationships, {})

    def test_an_empty_other_end_is_refused(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A1/rel/add",
                                    data=self.link_data(csrf, other_id="  "))
        self.assertEqual(response.status_code, 400)

    def test_linking_to_a_node_that_does_not_exist_is_refused(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A1/rel/add",
                                    data=self.link_data(csrf, other_id="W-missing"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.graph.relationships, {})

    def test_unlinking_removes_the_edge_and_tombstones_it(self):
        csrf = self.sign_in()
        self.client.post("/nodes/Person/A1/rel/add", data=self.link_data(csrf))
        response = self.client.post("/nodes/Person/A1/rel/delete", data=self.link_data(csrf))
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)
        # Here the decision does matter: MERGE would rebuild this edge.
        (override,) = active_overrides(self.db)
        self.assertEqual(override["kind"], "rel")
        self.assertEqual(override["rel_type"], "AUTHORED")
        self.assertEqual(override["actor"], "user:roman")

    def test_unlinking_something_that_is_not_linked_is_404_and_records_nothing(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A1/rel/delete", data=self.link_data(csrf))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_the_node_page_offers_only_the_links_its_label_allows(self):
        self.sign_in()
        body = self.client.get("/nodes/Person/A1").text
        self.assertIn("Person|AUTHORED|Publication", body)
        self.assertNotIn("Publication|MENTIONS_LINK|Repository", body)

    def test_a_viewer_can_neither_link_nor_unlink(self):
        csrf = self.sign_in(login="guest")
        for path in ("/nodes/Person/A1/rel/add", "/nodes/Person/A1/rel/delete"):
            self.assertEqual(self.client.post(path, data=self.link_data(csrf)).status_code, 403)
        self.assertEqual(self.graph.relationships, {})

    def test_linking_without_the_csrf_token_is_refused(self):
        self.sign_in()
        response = self.client.post("/nodes/Person/A1/rel/add",
                                    data={"triple": "Person|AUTHORED|Publication", "other_id": "W1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.graph.relationships, {})

    def test_the_page_node_is_placed_on_whichever_end_matches_its_label(self):
        # Opened from the publication's side, the person is the one typed
        # in and the publication is still the target.
        csrf = self.sign_in()
        self.client.post("/nodes/Publication/W1/rel/add",
                         data={"csrf": csrf, "triple": "Person|AUTHORED|Publication",
                               "other_id": "A1"})
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)
