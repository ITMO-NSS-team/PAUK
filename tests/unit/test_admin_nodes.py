import re
import unittest
from urllib.parse import quote

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import deps
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user, session_key
from pauk.graph.mutations import RELATIONSHIPS
from pauk.graph.overrides import COLLECTION, active_overrides, tombstoned_relationships
from pauk.settings import Settings
from tests.unit.test_mutations import FakeGraph


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

    def close(self):
        """The panel closes its client per request; the real one has this."""

    def list_nodes(self, label, fields, limit=50):
        rows = [{"id": node_id, **{name: props.get(name) for name in fields}}
                for (node_label, node_id), props in self.nodes.items() if node_label == label]
        return sorted(rows, key=lambda row: row["id"])[:limit]

    def _by_match(self, label, match_value):
        """The node an edge points at, found the way the loader finds it.

        Edges are stored keyed by whatever addresses the target — a url for
        a Repository, a login for a GitHubProfile — while the real client
        reports the other end's id. The fake used to return the match value
        as `other_id`, which is exactly the difference the panel got wrong,
        so it could never fail here.
        """
        for (node_label, node_id), props in self.nodes.items():
            if node_label != label:
                continue
            if node_id == match_value or match_value in props.values():
                return node_id, props
        return match_value, {}

    def fetch_node_relationships(self, label, node_id):
        found = []
        for src_label, rel_type, tgt_label, src_id, tgt_id in self.relationships:
            if src_label == label and src_id == node_id:
                other_id, props = self._by_match(tgt_label, tgt_id)
                found.append({"type": rel_type, "labels": [tgt_label], "other_id": other_id,
                              "other_props": props, "outgoing": True})
            elif tgt_label == label:
                # Which property the edge is stored against, the way the
                # loader decides it. Comparing against every value of the
                # node instead would attach an edge matched by `url` to a
                # node that merely has the same text in its `name`.
                match_prop = RELATIONSHIPS.get((src_label, rel_type, tgt_label), "id")
                mine = self.nodes.get((label, node_id), {})
                if tgt_id != (node_id if match_prop == "id" else mine.get(match_prop)):
                    continue
                other_props = self.nodes.get((src_label, src_id), {})
                found.append({"type": rel_type, "labels": [src_label], "other_id": src_id,
                              "other_props": other_props, "outgoing": False})
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
        return self.db[SESSIONS].find_one({"_id": session_key(token)})["csrf"]

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

    def test_an_empty_query_lists_what_is_there(self):
        # An empty box means "show me what exists" rather than "find
        # nothing": on an empty graph the difference is between a blank
        # page and seeing that it is in fact empty.
        self.sign_in()
        body = self.client.get("/nodes/Person").text
        self.assertIn("A1", body)
        self.assertIn("A2", body)
        self.assertIn("Показаны 2", body)

    def test_the_listing_says_when_it_is_only_the_first_page(self):
        from pauk.graph.mutations import SEARCH_LIMIT
        for n in range(SEARCH_LIMIT + 10):
            self.graph.nodes[("Repository", f"R{n:03}")] = {"id": f"R{n:03}", "url": f"u{n}"}
        self.sign_in()
        body = self.client.get("/nodes/Repository").text
        self.assertIn(f"первые {SEARCH_LIMIT}", body)

    def test_an_empty_label_says_so_rather_than_showing_a_blank_page(self):
        self.sign_in()
        self.assertIn("пока нет", self.client.get("/nodes/Repository").text)

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
        response = self.client.post("/nodes/Person/delete/A2", data={"csrf": csrf})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Person", "A2"), self.graph.nodes)
        (override,) = active_overrides(self.db)
        self.assertEqual(override["op"], "delete")

    def test_a_refused_delete_leaves_no_tombstone_behind(self):
        # A node with relationships is refused without cascade; a tombstone
        # recorded anyway would delete it on the next publish.
        csrf = self.sign_in()
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")] = {}
        response = self.client.post("/nodes/Person/delete/A1", data={"csrf": csrf})
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
        return self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def link_data(self, csrf, **extra):
        return {"csrf": csrf, "triple": "Person|AUTHORED|Publication",
                "other_id": "W1", **extra}

    def test_a_link_is_created_between_two_nodes(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/rel/add/A1", data=self.link_data(csrf))
        self.assertEqual(response.status_code, 303)
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)

    def test_a_created_link_needs_no_override_to_survive_publishing(self):
        # The loader never removes edges it does not know about, so there
        # is nothing for a decision to reapply.
        csrf = self.sign_in()
        self.client.post("/nodes/Person/rel/add/A1", data=self.link_data(csrf))
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_a_relationship_outside_the_eleven_known_triples_is_refused(self):
        # A malformed triple is a 400 — the form cannot produce one, so it
        # means the request was hand-made. An unknown but well-formed triple
        # is refused by the mutation layer and comes back on the page.
        csrf = self.sign_in()
        for triple in ("nonsense", "Person|AUTHORED"):
            response = self.client.post(
                "/nodes/Person/rel/add/A1",
                data={"csrf": csrf, "triple": triple, "other_id": "W1"})
            self.assertEqual(response.status_code, 400, triple)
        for triple in ("Person|OWNS|Publication", "Person|AUTHORED|Malicious"):
            response = self.client.post(
                "/nodes/Person/rel/add/A1",
                data={"csrf": csrf, "triple": triple, "other_id": "W1"})
            self.assertEqual(response.status_code, 303, triple)
            self.assertIn("error=", response.headers["location"], triple)
        self.assertEqual(self.graph.relationships, {})

    def test_an_empty_other_end_is_refused(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/rel/add/A1",
                                    data=self.link_data(csrf, other_id="  "))
        self.assertEqual(response.status_code, 400)

    def test_linking_to_a_node_that_does_not_exist_returns_with_a_reason(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/rel/add/A1",
                                    data=self.link_data(csrf, other_id="W-missing"))
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.graph.relationships, {})

    def test_unlinking_removes_the_edge_and_tombstones_it(self):
        csrf = self.sign_in()
        self.client.post("/nodes/Person/rel/add/A1", data=self.link_data(csrf))
        response = self.client.post("/nodes/Person/rel/delete/A1", data=self.link_data(csrf))
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)
        # Here the decision does matter: MERGE would rebuild this edge.
        (override,) = active_overrides(self.db)
        self.assertEqual(override["kind"], "rel")
        self.assertEqual(override["rel_type"], "AUTHORED")
        self.assertEqual(override["actor"], "user:roman")

    def test_unlinking_something_that_is_not_linked_is_404_and_records_nothing(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/rel/delete/A1", data=self.link_data(csrf))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_the_node_page_offers_only_the_links_its_label_allows(self):
        self.sign_in()
        body = self.client.get("/nodes/Person/A1").text
        self.assertIn("Person|AUTHORED|Publication", body)
        self.assertNotIn("Publication|MENTIONS_LINK|Repository", body)

    def test_a_viewer_can_neither_link_nor_unlink(self):
        csrf = self.sign_in(login="guest")
        for path in ("/nodes/Person/rel/add/A1", "/nodes/Person/rel/delete/A1"):
            self.assertEqual(self.client.post(path, data=self.link_data(csrf)).status_code, 403)
        self.assertEqual(self.graph.relationships, {})

    def test_linking_without_the_csrf_token_is_refused(self):
        self.sign_in()
        response = self.client.post("/nodes/Person/rel/add/A1",
                                    data={"triple": "Person|AUTHORED|Publication", "other_id": "W1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.graph.relationships, {})

    def test_the_page_node_is_placed_on_whichever_end_matches_its_label(self):
        # Opened from the publication's side, the person is the one typed
        # in and the publication is still the target.
        csrf = self.sign_in()
        self.client.post("/nodes/Publication/rel/add/W1",
                         data={"csrf": csrf, "triple": "Person|AUTHORED|Publication",
                               "other_id": "A1"})
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)


class CreateNodeTest(unittest.TestCase):
    """Adding a node the pipeline does not know about."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1", "name_en": "Ivan Petrov"}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})
        return self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def test_the_form_is_not_mistaken_for_a_node_called_new(self):
        # /nodes/Person/new must reach the form, not a lookup for a node
        # whose id happens to be "new".
        self.sign_in()
        response = self.client.get("/nodes/Person/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Новый Person", response.text)

    def test_a_node_is_created_and_the_page_opens_on_it(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/new",
                                    data={"csrf": csrf, "id": "A9", "name_en": "New Person"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/nodes/Person/A9?created=1")
        self.assertEqual(self.graph.nodes[("Person", "A9")]["name_en"], "New Person")

    def test_a_hand_made_node_needs_no_override_to_survive_publishing(self):
        # The loader only touches ids it has rows for, so an invented id is
        # never overwritten and there is nothing to reapply.
        csrf = self.sign_in()
        self.client.post("/nodes/Person/new", data={"csrf": csrf, "id": "A9"})
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_a_node_without_an_id_is_refused(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/new", data={"csrf": csrf, "id": "  "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.graph.nodes), 1)

    def test_an_id_that_is_taken_is_refused_rather_than_overwriting(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/new",
                                    data={"csrf": csrf, "id": "A1", "name_en": "Impostor"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_en"], "Ivan Petrov")

    def test_empty_boxes_are_left_out_rather_than_stored_as_nothing(self):
        csrf = self.sign_in()
        self.client.post("/nodes/Person/new",
                         data={"csrf": csrf, "id": "A9", "name_en": "New", "orcid": ""})
        self.assertNotIn("orcid", self.graph.nodes[("Person", "A9")])

    def test_a_field_outside_the_whitelist_is_dropped(self):
        csrf = self.sign_in()
        self.client.post("/nodes/Person/new",
                         data={"csrf": csrf, "id": "A9", "name_en": "New", "is_admin": "yes"})
        self.assertNotIn("is_admin", self.graph.nodes[("Person", "A9")])

    def test_an_unknown_label_never_reaches_the_graph(self):
        csrf = self.sign_in()
        self.graph.calls.clear()
        response = self.client.post("/nodes/Malicious/new", data={"csrf": csrf, "id": "X"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.graph.calls, [])

    def test_creating_without_the_csrf_token_is_refused(self):
        self.sign_in()
        response = self.client.post("/nodes/Person/new", data={"id": "A9"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(("Person", "A9"), self.graph.nodes)

    def test_a_viewer_sees_no_way_in_and_is_refused_at_the_door(self):
        csrf = self.sign_in(login="guest")
        self.assertNotIn("/nodes/Person/new", self.client.get("/nodes/Person").text)
        self.assertEqual(self.client.get("/nodes/Person/new").status_code, 403)
        self.assertEqual(
            self.client.post("/nodes/Person/new", data={"csrf": csrf, "id": "A9"}).status_code, 403)

    def test_an_editor_is_offered_the_button(self):
        self.sign_in()
        self.assertIn("/nodes/Person/new", self.client.get("/nodes/Person").text)


class DeleteConfirmationTest(unittest.TestCase):
    """Deleting is irreversible, so the page asks before submitting."""

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
        return self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def test_the_page_asks_before_deleting(self):
        self.sign_in()
        body = self.client.get("/nodes/Person/A1").text
        self.assertIn("confirm(", body)
        self.assertIn("Удалить Person A1?", body)
        self.assertIn("необратимо", body)

    def test_the_warning_counts_the_links_that_go_with_it(self):
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")] = {}
        self.sign_in()
        body = self.client.get("/nodes/Person/A1").text
        self.assertIn("1 связь", body)

    def test_a_viewer_is_shown_no_delete_form_at_all(self):
        self.sign_in(login="guest")
        body = self.client.get("/nodes/Person/A1").text
        self.assertNotIn("delete-form", body)
        self.assertNotIn("confirm(", body)

    def test_the_confirmation_is_not_the_only_thing_standing_in_the_way(self):
        # A prompt lives in the browser and can be skipped by posting
        # directly, so the server checks still have to hold on their own.
        self.sign_in(login="guest")
        csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]
        response = self.client.post("/nodes/Person/delete/A1", data={"csrf": csrf})
        self.assertEqual(response.status_code, 403)
        self.assertIn(("Person", "A1"), self.graph.nodes)


class LinkMistakeTest(unittest.TestCase):
    """What the panel says when the other end cannot be found."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1"}
        self.graph.nodes[("Publication", "W1")] = {"id": "W1"}
        self.graph.nodes[("Repository", "R1")] = {
            "id": "R1", "url": "https://github.com/itmo/pauk"}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def link(self, page, triple, other):
        label, node_id = page.split("/", 1)
        return self.client.post(f"/nodes/{label}/rel/add/{node_id}",
                                data={"csrf": self.csrf, "triple": triple, "other_id": other})

    def reason(self, response):
        from urllib.parse import unquote
        location = response.headers.get("location", "")
        return unquote(location.split("error=")[1]) if "error=" in location else ""

    def test_a_missing_other_end_returns_to_the_form_not_a_json_error(self):
        # A typo is an ordinary mistake: the person needs the form back,
        # with the reason, rather than a bare 400 page.
        response = self.link("Person/A1", "Person|AUTHORED|Publication", "W-999")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/nodes/Person/A1?error="))
        self.assertIn("W-999", self.reason(response))

    def test_an_id_pasted_where_a_url_is_wanted_says_which_field_to_use(self):
        # Two of the eleven links match on something other than an id, and
        # this is the mistake people make.
        response = self.link("Publication/W1", "Publication|MENTIONS_LINK|Repository", "R1")
        reason = self.reason(response)
        self.assertIn("url", reason)
        self.assertIn("адрес репозитория", reason)

    def test_an_id_pasted_where_a_login_is_wanted_says_so_too(self):
        response = self.link("Repository/R1", "Repository|OWNED_BY|GitHubProfile", "12345")
        reason = self.reason(response)
        self.assertIn("login", reason)
        self.assertIn("логин", reason)

    def test_the_right_value_still_links(self):
        response = self.link("Publication/W1", "Publication|MENTIONS_LINK|Repository",
                             "https://github.com/itmo/pauk")
        self.assertEqual(response.headers["location"], "/nodes/Publication/W1?linked=1")

    def test_the_reason_is_shown_on_the_page(self):
        body = self.client.get("/nodes/Person/A1", params={"error": "нет такого узла"}).text
        self.assertIn("нет такого узла", body)

    def test_the_form_says_what_to_type_before_the_mistake_happens(self):
        body = self.client.get("/nodes/Repository/R1").text
        self.assertIn("по login", body)
        self.assertIn("для репозитория — адрес", body)


class LinkDirectionTest(unittest.TestCase):
    """Which side the open node is on, and how the other one is addressed."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.nodes[("Repository", "R1")] = {
            "id": "R1", "url": "https://github.com/itmo/pauk"}
        self.graph.nodes[("Publication", "W1")] = {"id": "W1"}
        self.graph.nodes[("GitHubProfile", "G1")] = {"id": "G1", "login": "octocat"}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def link(self, page, triple, other):
        label, node_id = page.split("/", 1)
        return self.client.post(f"/nodes/{label}/rel/add/{node_id}",
                                data={"csrf": self.csrf, "triple": triple, "other_id": other})

    def test_an_incoming_link_works_when_this_node_is_matched_by_a_url(self):
        # The link matches its target by url, and here the target is the
        # open repository — sending its id found nothing at all.
        response = self.link("Repository/R1", "Publication|MENTIONS_LINK|Repository", "W1")
        self.assertEqual(response.headers["location"], "/nodes/Repository/R1?linked=1")
        self.assertIn(("Publication", "MENTIONS_LINK", "Repository", "W1",
                       "https://github.com/itmo/pauk"), self.graph.relationships)

    def test_an_incoming_link_works_when_this_node_is_matched_by_a_login(self):
        response = self.link("GitHubProfile/G1", "Repository|OWNED_BY|GitHubProfile", "R1")
        self.assertEqual(response.headers["location"], "/nodes/GitHubProfile/G1?linked=1")
        self.assertIn(("Repository", "OWNED_BY", "GitHubProfile", "R1", "octocat"),
                      self.graph.relationships)

    def test_the_form_asks_for_an_id_when_the_other_end_is_the_source(self):
        # match_prop describes the target. On an incoming link the target is
        # this node, so telling the person to type a url would be wrong.
        body = self.client.get("/nodes/Repository/R1").text
        self.assertIn("упомянут в публикации — указать Publication по id", body)
        self.assertIn("принадлежит аккаунту — указать GitHubProfile по login", body)

    def test_a_node_missing_the_field_the_link_matches_on_says_so(self):
        self.graph.nodes[("Repository", "R2")] = {"id": "R2"}      # без url
        response = self.link("Repository/R2", "Publication|MENTIONS_LINK|Repository", "W1")
        from urllib.parse import unquote
        reason = unquote(response.headers["location"].split("error=")[1])
        self.assertIn("не заполнено поле url", reason)

    def test_links_are_shown_in_words_not_only_as_edge_types(self):
        self.graph.relationships[("Repository", "OWNED_BY", "GitHubProfile", "R1", "octocat")] = {}
        body = self.client.get("/nodes/Repository/R1").text
        self.assertIn("принадлежит аккаунту", body)
        self.assertIn("OWNED_BY", body)          # тип остаётся для сверки со схемой

    def test_the_phrase_is_read_from_the_side_you_are_looking_from(self):
        self.graph.relationships[("Repository", "OWNED_BY", "GitHubProfile", "R1", "octocat")] = {}
        self.assertIn("принадлежит аккаунту", self.client.get("/nodes/Repository/R1").text)
        self.assertIn("владеет репозиторием", self.client.get("/nodes/GitHubProfile/G1").text)


class ConcurrentEditTest(unittest.TestCase):
    """Two people editing one record must not overwrite each other."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        for who in ("roman", "petrov"):
            create_user(self.db, who, "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Person", "A1", name_ru="Иван")

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.app = app

    def open_form(self, login):
        """Sign in and read the page, as a person would before editing."""
        import re
        client = TestClient(self.app, follow_redirects=False)
        client.post("/login", data={"login": login, "password": "hunter2"})
        page = client.get("/nodes/Person/A1").text
        return client, {
            "csrf": re.search(r'name="csrf" value="([^"]+)"', page).group(1),
            "seen_at": re.search(r'name="seen_at" value="([^"]*)"', page).group(1)}

    def test_the_second_save_is_refused_rather_than_silently_winning(self):
        # Both open the same record; the first saves, then the second.
        # Without this the second write simply replaced the first and
        # nobody was told.
        first, form_first = self.open_form("roman")
        second, form_second = self.open_form("petrov")

        first.post("/nodes/Person/A1", data={**form_first, "name_ru": "Пётр"})
        response = second.post("/nodes/Person/A1", data={**form_second, "name_ru": "Иоанн"})

        self.assertIn("stale=1", response.headers["location"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Пётр")

    def test_the_refusal_is_explained_on_the_page(self):
        client, _ = self.open_form("roman")
        body = client.get("/nodes/Person/A1", params={"stale": "1"}).text
        self.assertIn("изменил кто-то ещё", body)

    def test_reopening_the_page_lets_the_edit_go_through(self):
        first, form_first = self.open_form("roman")
        second, form_second = self.open_form("petrov")
        first.post("/nodes/Person/A1", data={**form_first, "name_ru": "Пётр"})
        second.post("/nodes/Person/A1", data={**form_second, "name_ru": "Иоанн"})

        # Reading the page again picks up the new updated_at. The client
        # has to be the one that read it: the csrf token belongs to that
        # session, not to the earlier one.
        again, fresh = self.open_form("petrov")
        response = again.post("/nodes/Person/A1", data={**fresh, "name_ru": "Иоанн"})
        self.assertIn("saved=1", response.headers["location"])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иоанн")

    def test_the_form_carries_the_moment_it_was_rendered(self):
        _, form = self.open_form("roman")
        self.assertEqual(form["seen_at"], str(self.graph.nodes[("Person", "A1")]["updated_at"]))

    def test_an_edit_still_works_when_nobody_else_touched_the_record(self):
        client, form = self.open_form("roman")
        response = client.post("/nodes/Person/A1", data={**form, "name_ru": "Пётр"})
        self.assertIn("saved=1", response.headers["location"])


class UnlinkByMatchFieldTest(unittest.TestCase):
    """Removing an edge addressed by something other than an id.

    Two of the eleven relationships are matched that way — a Repository by
    url, a GitHubProfile by login. Sending the other end's id finds no edge
    at all, and the panel answered "there is no such link" for a link that
    plainly existed.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Repository", "R1", url="https://github.com/itmo/pauk", name="pauk")
        self.graph.add("Publication", "W1", title="A paper")
        self.graph.add("GitHubProfile", "G1", login="octocat")
        self.graph.relationships[
            ("Publication", "MENTIONS_LINK", "Repository", "W1",
             "https://github.com/itmo/pauk")] = {}
        self.graph.relationships[
            ("Repository", "OWNED_BY", "GitHubProfile", "R1", "octocat")] = {}

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def form_value(self, page, triple):
        """What the page would submit to unlink one particular edge.

        Picked by triple rather than by position: a record can have several
        links, and the first form on the page is not necessarily the one
        under test.
        """
        import re
        body = self.client.get(page).text
        for block in body.split("/rel/delete")[1:]:
            if f'name="triple"\n                 value="{triple}"' in block or \
               f'value="{triple}"' in block:
                return re.search(r'name="other_id" value="([^"]*)"', block).group(1)
        raise AssertionError(f"на странице нет формы отвязки для {triple}")

    def test_the_page_offers_the_value_the_edge_is_matched_by(self):
        # From the publication's side the repository is addressed by url.
        self.assertEqual(
            self.form_value("/nodes/Publication/W1", "Publication|MENTIONS_LINK|Repository"),
            "https://github.com/itmo/pauk")

    def test_a_repository_link_is_removed_from_the_publication_side(self):
        response = self.client.post("/nodes/Publication/rel/delete/W1", data={
            "csrf": self.csrf, "triple": "Publication|MENTIONS_LINK|Repository",
            "other_id": "https://github.com/itmo/pauk"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Publication", "MENTIONS_LINK", "Repository", "W1",
                          "https://github.com/itmo/pauk"), self.graph.relationships)

    def test_a_profile_link_is_removed_from_the_repository_side(self):
        self.assertEqual(
            self.form_value("/nodes/Repository/R1", "Repository|OWNED_BY|GitHubProfile"),
            "octocat")
        response = self.client.post("/nodes/Repository/rel/delete/R1", data={
            "csrf": self.csrf, "triple": "Repository|OWNED_BY|GitHubProfile",
            "other_id": "octocat"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Repository", "OWNED_BY", "GitHubProfile", "R1", "octocat"),
                         self.graph.relationships)

    def test_the_tombstone_stores_the_value_the_loader_compares(self):
        # Second-order fault: even a successful delete would be undone by
        # the next publish if the tombstone held an id, because the loader
        # compares it against the prepared row's url.
        self.client.post("/nodes/Publication/rel/delete/W1", data={
            "csrf": self.csrf, "triple": "Publication|MENTIONS_LINK|Repository",
            "other_id": "https://github.com/itmo/pauk"})
        (override,) = active_overrides(self.db)
        self.assertEqual(override["target_id"], "https://github.com/itmo/pauk")

    def test_an_id_matched_link_still_works(self):
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")] = {}
        self.graph.add("Person", "A1")
        self.assertEqual(
            self.form_value("/nodes/Person/A1", "Person|AUTHORED|Publication"), "W1")


class FieldTypeTest(unittest.TestCase):
    """A form submits text for every box, including the untouched ones."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Repository", "R1", name="PAUK", stars_num=42,
                       has_readme=True, cited_urls=["https://a.test"])

        app = build(Settings(), self.db)
        from pauk.admin import deps
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def submit(self, **over):
        """Send the form the way a browser does — every box, as text."""
        import re
        page = self.client.get("/nodes/Repository/R1").text
        data = {"csrf": re.search(r'name="csrf" value="([^"]+)"', page).group(1),
                "seen_at": re.search(r'name="seen_at" value="([^"]*)"', page).group(1),
                "name": "PAUK", "stars_num": "42", "has_readme": "True",
                "cited_urls": '["https://a.test"]'}
        data.update(over)
        return self.client.post("/nodes/Repository/R1", data=data)

    def test_untouched_fields_keep_their_types(self):
        # "42" is not 42, so an untouched count counted as an edit and was
        # written back as a string.
        self.submit(name="PAUK 2")
        node = self.graph.nodes[("Repository", "R1")]
        self.assertIsInstance(node["stars_num"], int)
        self.assertIsInstance(node["has_readme"], bool)
        self.assertIsInstance(node["cited_urls"], list)

    def test_only_the_edited_field_is_recorded(self):
        self.submit(name="PAUK 2")
        (override,) = list(self.db["graph_overrides"].find())
        self.assertEqual(list(override["fields"]), ["name"])

    def test_a_number_edited_by_hand_stays_a_number(self):
        self.submit(stars_num="43")
        self.assertEqual(self.graph.nodes[("Repository", "R1")]["stars_num"], 43)

    def test_a_boolean_reads_the_words_a_person_would_type(self):
        self.submit(has_readme="false")
        self.assertIs(self.graph.nodes[("Repository", "R1")]["has_readme"], False)

    def test_a_list_is_given_as_json(self):
        self.submit(cited_urls='["https://a.test", "https://b.test"]')
        self.assertEqual(self.graph.nodes[("Repository", "R1")]["cited_urls"],
                         ["https://a.test", "https://b.test"])

    def test_text_that_only_looks_like_a_number_is_left_alone(self):
        # The type comes from the field, not from the shape of the input:
        # a name of "2024" is a name.
        self.submit(name="2024")
        self.assertEqual(self.graph.nodes[("Repository", "R1")]["name"], "2024")

    def test_clearing_a_box_stores_nothing(self):
        self.submit(stars_num="")
        self.assertIsNone(self.graph.nodes[("Repository", "R1")]["stars_num"])

    def test_a_new_node_reads_values_as_json(self):
        # Nothing to take a type from, so the rule is the CLI's: 42 is a
        # number, true is a boolean, everything else is text.
        import re
        page = self.client.get("/nodes/Repository/new").text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        self.client.post("/nodes/Repository/new", data={
            "csrf": csrf, "id": "R9", "name": "new", "stars_num": "7", "has_readme": "true"})
        node = self.graph.nodes[("Repository", "R9")]
        self.assertEqual(node["stars_num"], 7)
        self.assertIs(node["has_readme"], True)
        self.assertEqual(node["name"], "new")


class UrlAsIdTest(unittest.TestCase):
    """A LinkCandidate is identified by the address it was found at.

    That id carries slashes, and can carry "?" and "#" too, so every
    screen has to survive a node whose id is a URL: the routes match it
    with `{node_id:path}`, and the pages percent-encode it on the way out.
    """

    URL = "https://github.com/org/repo?ref=main#readme"

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("LinkCandidate", self.URL, url=self.URL, host="github.com")
        self.graph.add("Publication", "W1", title="Статья")
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]
        self.path = quote(self.URL, safe="/")

    def test_the_search_links_to_a_page_that_opens(self):
        page = self.client.get("/nodes/LinkCandidate", params={"q": "github"}).text
        found = [href for href in re.findall(r'href="(/nodes/LinkCandidate/[^"]*)"', page)
                 if not href.endswith("/new")]
        self.assertTrue(found, "поиск не дал ссылки на узел")
        self.assertEqual(self.client.get(found[0]).status_code, 200)

    def test_the_id_is_encoded_in_the_link(self):
        # Unencoded, the browser reads "?ref=main" as a query and drops
        # the rest of the id before the request is even sent.
        page = self.client.get("/nodes/LinkCandidate", params={"q": "github"}).text
        self.assertIn("%3Fref%3Dmain%23readme", page)

    def test_the_page_opens(self):
        response = self.client.get(f"/nodes/LinkCandidate/{self.path}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("github.com", response.text)

    def test_editing_saves(self):
        seen = self.graph.nodes[("LinkCandidate", self.URL)]["updated_at"]
        response = self.client.post(f"/nodes/LinkCandidate/{self.path}", data={
            "csrf": self.csrf, "host": "gitlab.com", "url": self.URL, "seen_at": seen})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.graph.nodes[("LinkCandidate", self.URL)]["host"], "gitlab.com")

    def test_the_redirect_after_an_edit_opens(self):
        seen = self.graph.nodes[("LinkCandidate", self.URL)]["updated_at"]
        response = self.client.post(f"/nodes/LinkCandidate/{self.path}", data={
            "csrf": self.csrf, "host": "gitlab.com", "url": self.URL, "seen_at": seen})
        self.assertEqual(self.client.get(response.headers["location"]).status_code, 200)

    def test_deleting_works(self):
        response = self.client.post(f"/nodes/LinkCandidate/delete/{self.path}",
                                    data={"csrf": self.csrf})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("LinkCandidate", self.URL), self.graph.nodes)

    def test_restoring_works(self):
        self.client.post(f"/nodes/LinkCandidate/delete/{self.path}", data={"csrf": self.csrf})
        response = self.client.post(f"/nodes/LinkCandidate/restore/{self.path}",
                                    data={"csrf": self.csrf})
        self.assertEqual(response.status_code, 303)
        self.assertIn(("LinkCandidate", self.URL), self.graph.nodes)

    def test_linking_and_unlinking_work(self):
        triple = "Publication|MENTIONS_LINK|LinkCandidate"
        self.client.post("/nodes/Publication/rel/add/W1", data={
            "csrf": self.csrf, "triple": triple, "other_id": self.URL})
        self.assertEqual(len(self.graph.relationships), 1)
        self.client.post("/nodes/Publication/rel/delete/W1", data={
            "csrf": self.csrf, "triple": triple, "other_id": self.URL})
        self.assertEqual(len(self.graph.relationships), 0)

    def test_an_action_is_not_read_as_part_of_the_id(self):
        # The path converter is greedy: with the action at the end,
        # "/rel/delete" went to the node-delete route, which read it as a
        # node called "W1/rel" and answered that no such node exists.
        triple = "Publication|MENTIONS_LINK|LinkCandidate"
        self.client.post("/nodes/Publication/rel/add/W1", data={
            "csrf": self.csrf, "triple": triple, "other_id": self.URL})
        response = self.client.post("/nodes/Publication/rel/delete/W1", data={
            "csrf": self.csrf, "triple": triple, "other_id": self.URL})
        self.assertEqual(response.status_code, 303)
        self.assertIn(("Publication", "W1"), self.graph.nodes)
        self.assertIsNone(self.db[COLLECTION].find_one({"_id": "node:Publication:W1/rel"}))

    def test_an_address_ending_in_delete_is_still_a_node(self):
        ending = "https://example.org/api/delete"
        self.graph.add("LinkCandidate", ending, url=ending, host="example.org")
        response = self.client.get(f"/nodes/LinkCandidate/{quote(ending, safe='/')}")
        self.assertEqual(response.status_code, 200)


class VanishedRecordTest(unittest.TestCase):
    """Saving a form whose record was deleted meanwhile.

    read_node used to sit above the try, so NotFound escaped the handler
    and the save answered 500 instead of saying what happened.
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
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def test_the_save_is_answered_not_crashed(self):
        self.graph.nodes.pop(("Person", "A1"))
        response = self.client.post("/nodes/Person/A1",
                                    data={"csrf": self.csrf, "name_ru": "Иван"})
        self.assertEqual(response.status_code, 303)

    def test_it_lands_on_the_record_s_own_page(self):
        self.graph.nodes.pop(("Person", "A1"))
        response = self.client.post("/nodes/Person/A1",
                                    data={"csrf": self.csrf, "name_ru": "Иван"})
        self.assertEqual(response.headers["location"], "/nodes/Person/A1")

    def test_nothing_is_recorded_as_a_decision(self):
        self.graph.nodes.pop(("Person", "A1"))
        self.client.post("/nodes/Person/A1", data={"csrf": self.csrf, "name_ru": "Иван"})
        self.assertEqual(list(active_overrides(self.db)), [])


class UnlinkFromTheTargetTest(unittest.TestCase):
    """Two links address their target by something other than an id.

    Seen from the target's own page the other end is the *source*, which is
    addressed by id — and the target is addressed by its url or its login,
    not by the id the page is opened under. Getting either side wrong finds
    no edge and the link cannot be removed at all.
    """

    URL = "https://github.com/org/repo"

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        self.graph.add("Publication", "W1", title="Статья")
        self.graph.add("Repository", "R1", url=self.URL, name="repo")
        self.graph.add("GitHubProfile", "G1", login="octocat")
        self.graph.relationships[
            ("Publication", "MENTIONS_LINK", "Repository", "W1", self.URL)] = {}
        self.graph.relationships[
            ("Repository", "OWNED_BY", "GitHubProfile", "R1", "octocat")] = {}
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def form_on(self, label, node_id, triple):
        """The unlink form the page renders for one edge, as it would be sent.

        Picked by its triple: a node has several edges, and taking whichever
        form comes first tests a different one than intended.
        """
        page = self.client.get(f"/nodes/{label}/{node_id}").text
        for action, body in re.findall(
                r'<form method="post" action="([^"]*rel/delete[^"]*)">(.*?)</form>', page, re.S):
            fields = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', body, re.S))
            if fields.get("triple") == triple:
                return action, fields
        raise AssertionError(f"на странице нет формы отвязывания для {triple}")

    def test_the_form_offers_the_source_id_not_a_missing_property(self):
        _, fields = self.form_on("Repository", "R1", "Publication|MENTIONS_LINK|Repository")
        self.assertEqual(fields["other_id"], "W1")

    def test_unlinking_a_url_matched_link_from_the_target(self):
        action, fields = self.form_on("Repository", "R1", "Publication|MENTIONS_LINK|Repository")
        response = self.client.post(action, data={**fields, "csrf": self.csrf})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Publication", "MENTIONS_LINK", "Repository", "W1", self.URL),
                         self.graph.relationships)

    def test_unlinking_a_login_matched_link_from_the_target(self):
        action, fields = self.form_on("GitHubProfile", "G1", "Repository|OWNED_BY|GitHubProfile")
        response = self.client.post(action, data={**fields, "csrf": self.csrf})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(("Repository", "OWNED_BY", "GitHubProfile", "R1", "octocat"),
                         self.graph.relationships)

    def test_the_decision_names_the_edge_the_way_the_loader_does(self):
        action, fields = self.form_on("Repository", "R1", "Publication|MENTIONS_LINK|Repository")
        self.client.post(action, data={**fields, "csrf": self.csrf})
        self.assertEqual(
            tombstoned_relationships(self.db),
            {("Publication", "MENTIONS_LINK", "Repository", "W1", self.URL)})

    def test_an_empty_match_field_is_refused_rather_than_silently_missing(self):
        # Posted straight at the route rather than read off the page: the
        # graph keeps the edge on the node itself, while the double here
        # stores it against the match value, so clearing `url` hides the
        # row in the double although a real graph would still show it.
        self.graph.nodes[("Repository", "R1")]["url"] = None
        response = self.client.post("/nodes/Repository/rel/delete/R1", data={
            "csrf": self.csrf, "triple": "Publication|MENTIONS_LINK|Repository",
            "other_id": "W1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("url", response.json()["detail"])


class UnaddressableIdTest(unittest.TestCase):
    """An id the panel could create and then never open again."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.graph = FakePanelGraph()
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(self.client.cookies[COOKIE])})["csrf"]

    def create(self, node_id):
        return self.client.post("/nodes/Person/new",
                                data={"csrf": self.csrf, "id": node_id, "name_ru": "Иван"})

    def test_a_line_break_is_refused(self):
        self.assertEqual(self.create("A1\nвторая строка").status_code, 400)
        self.assertEqual(self.graph.nodes, {})

    def test_a_tab_is_refused(self):
        self.assertEqual(self.create("A1\tX").status_code, 400)

    def test_an_ordinary_id_still_works(self):
        self.assertEqual(self.create("A1").status_code, 303)
        self.assertIn(("Person", "A1"), self.graph.nodes)

    def test_a_url_is_still_a_fine_id(self):
        # LinkCandidate ids are addresses; only control characters are out.
        response = self.client.post(
            "/nodes/LinkCandidate/new",
            data={"csrf": self.csrf, "id": "https://a.example/b?c=1", "url": "https://a.example/b"})
        self.assertEqual(response.status_code, 303)
