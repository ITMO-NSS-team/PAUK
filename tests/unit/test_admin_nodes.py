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

    def list_nodes(self, label, fields, limit=50):
        rows = [{"id": node_id, **{name: props.get(name) for name in fields}}
                for (node_label, node_id), props in self.nodes.items() if node_label == label]
        return sorted(rows, key=lambda row: row["id"])[:limit]

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
        # A malformed triple is a 400 — the form cannot produce one, so it
        # means the request was hand-made. An unknown but well-formed triple
        # is refused by the mutation layer and comes back on the page.
        csrf = self.sign_in()
        for triple in ("nonsense", "Person|AUTHORED"):
            response = self.client.post(
                "/nodes/Person/A1/rel/add",
                data={"csrf": csrf, "triple": triple, "other_id": "W1"})
            self.assertEqual(response.status_code, 400, triple)
        for triple in ("Person|OWNS|Publication", "Person|AUTHORED|Malicious"):
            response = self.client.post(
                "/nodes/Person/A1/rel/add",
                data={"csrf": csrf, "triple": triple, "other_id": "W1"})
            self.assertEqual(response.status_code, 303, triple)
            self.assertIn("error=", response.headers["location"], triple)
        self.assertEqual(self.graph.relationships, {})

    def test_an_empty_other_end_is_refused(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A1/rel/add",
                                    data=self.link_data(csrf, other_id="  "))
        self.assertEqual(response.status_code, 400)

    def test_linking_to_a_node_that_does_not_exist_returns_with_a_reason(self):
        csrf = self.sign_in()
        response = self.client.post("/nodes/Person/A1/rel/add",
                                    data=self.link_data(csrf, other_id="W-missing"))
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["location"])
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
        return self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

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
        return self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

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
        csrf = self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]
        response = self.client.post("/nodes/Person/A1/delete", data={"csrf": csrf})
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
        self.csrf = self.db[SESSIONS].find_one({"_id": self.client.cookies[COOKIE]})["csrf"]

    def link(self, page, triple, other):
        return self.client.post(f"/nodes/{page}/rel/add",
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
