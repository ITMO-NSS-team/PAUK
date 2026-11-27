"""HTTP regressions for text repository names and recovery of legacy records."""

import copy
import unittest
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from pauk.admin import deps, nodes
from pauk.admin.app import build
from pauk.graph.overrides import COLLECTION, apply_overrides
from pauk.settings import Settings
from tests.unit.admin_forms import RenderedForms
from tests.unit.test_admin_nodes import FakePanelGraph


class RepositoryNameFormsTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().pauk
        self.graph = FakePanelGraph()
        app = build(Settings(neo4j_password=""), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        app.dependency_overrides[deps.get_session] = lambda: {
            "login": "editor", "role": "editor", "csrf": "test-csrf",
        }
        self.client = TestClient(app, follow_redirects=False)
        self.addCleanup(self.client.close)

    def form(self, node_id):
        response = self.client.get(f"/nodes/Repository/{node_id}")
        self.assertEqual(response.status_code, 200)
        return RenderedForms(response.text).with_button("Сохранить")

    def edit(self, node_id, **changes):
        form = self.form(node_id)
        form["fields"].update(changes)
        return self.client.post(form["action"], data=form["fields"])

    def add_repository(self, node_id, name):
        url = f"https://github.com/team/{node_id}"
        self.graph.add("Repository", node_id, name=name, url=url,
                       description="Keep description", stars_num=7, topics=["science"])
        self.graph.add("Publication", "W1", title="Source paper")
        edge = ("Publication", "MENTIONS_LINK", "Repository", "W1", url)
        self.graph.relationships[edge] = {"source": "manual"}
        return edge

    def test_creation_keeps_json_looking_names_as_literal_text(self):
        for index, name in enumerate(("123", "001", "0", "true", "false", "null", "NaN", "1.25",
                                      "1e3", '["repo"]', '{"repo":1}', '"quoted"', " Репозиторий ")):
            with self.subTest(name=name):
                node_id = f"created-{index}"
                response = self.client.post("/nodes/Repository/new", data={
                    "csrf": "test-csrf", "id": node_id, "name": name,
                    "url": f"https://github.com/team/{node_id}", "stars_num": "7",
                    "topics": '["science"]',
                })
                self.assertEqual(response.status_code, 303, response.text)
                stored = self.graph.nodes["Repository", node_id]
                self.assertIs(type(stored["name"]), str)
                self.assertEqual(stored["name"], name.strip())
                self.assertEqual(stored["stars_num"], 7)
                self.assertEqual(stored["topics"], ["science"])
                decision = self.db[COLLECTION].find_one({"target_id": node_id})
                self.assertEqual(decision["fields"]["name"], name.strip())
                self.assertIs(type(decision["fields"]["name"]), str)

    def test_edit_repairs_legacy_name_types_without_losing_other_fields_or_links(self):
        for index, previous in enumerate((True, False, 0, 123, 1.5, ["old"], {"old": True}, None)):
            with self.subTest(previous=previous):
                node_id = f"legacy-{index}"
                edge = self.add_repository(node_id, previous)
                response = self.edit(node_id, name="recovered")
                self.assertEqual(response.status_code, 303, response.text)
                row = self.graph.nodes["Repository", node_id]
                self.assertEqual(row["name"], "recovered")
                self.assertEqual(row["description"], "Keep description")
                self.assertEqual(row["stars_num"], 7)
                self.assertEqual(row["topics"], ["science"])
                self.assertEqual(self.graph.relationships[edge], {"source": "manual"})
                decision = self.db[COLLECTION].find_one({"target_id": node_id})
                self.assertEqual(decision["fields"], {"name": "recovered"})
                self.assertEqual(decision["auto_value"], {"name": previous})

    def test_changed_numeric_text_can_repair_a_numeric_legacy_name(self):
        self.add_repository("numeric", 0)
        response = self.edit("numeric", name=" 0 ")
        self.assertEqual(response.status_code, 303, response.text)
        self.assertIs(type(self.graph.nodes["Repository", "numeric"]["name"]), str)
        self.assertEqual(self.graph.nodes["Repository", "numeric"]["name"], "0")

    def test_unchanged_invalid_name_does_not_block_unrelated_edits(self):
        for index, previous in enumerate((True, False, 0, [None], {"old": True}, None, "", "   ")):
            with self.subTest(previous=previous):
                node_id = f"untouched-{index}"
                edge = self.add_repository(node_id, previous)
                response = self.edit(node_id, description="Changed description")
                self.assertEqual(response.status_code, 303, response.text)
                row = self.graph.nodes["Repository", node_id]
                self.assertIs(type(row["name"]), type(previous))
                self.assertEqual(row["name"], previous)
                self.assertEqual(row["description"], "Changed description")
                self.assertIn(edge, self.graph.relationships)
                self.assertEqual(self.db[COLLECTION].find_one({"target_id": node_id})["fields"],
                                 {"description": "Changed description"})

    def test_omitted_name_preserves_missing_name_when_editing_another_field(self):
        self.graph.add("Repository", "unnamed", description="Before")
        response = self.client.post("/nodes/Repository/unnamed", data={
            "csrf": "test-csrf", "description": "After",
        })
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("name", self.graph.nodes["Repository", "unnamed"])
        self.assertEqual(self.graph.nodes["Repository", "unnamed"]["description"], "After")

    def test_blank_name_rejects_whole_edit_and_preserves_links(self):
        edge = self.add_repository("named", "Keep")
        before = copy.deepcopy(self.graph.nodes)
        for name in ("", "  ", "\t\n"):
            with self.subTest(name=name):
                response = self.edit("named", name=name, description="Must not save")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.graph.nodes, before)
                self.assertEqual(self.graph.relationships[edge], {"source": "manual"})
                self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_blank_or_missing_name_cannot_create_a_repository(self):
        for fields in ({}, {"name": ""}, {"name": "  "}):
            with self.subTest(fields=fields):
                response = self.client.post("/nodes/Repository/new", data={
                    "csrf": "test-csrf", "id": "new", "url": "https://github.com/team/new", **fields,
                })
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(("Repository", "new"), self.graph.nodes)
                self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_numeric_name_survives_repeat_save_and_override_reapplication(self):
        self.add_repository("repeat", True)
        self.assertEqual(self.edit("repeat", name="001").status_code, 303)
        version = self.graph.nodes["Repository", "repeat"]["updated_at"]
        decision = copy.deepcopy(self.db[COLLECTION].find_one({"target_id": "repeat"}))
        self.assertIn("unchanged=1", self.edit("repeat").headers["location"])
        self.assertEqual(self.graph.nodes["Repository", "repeat"]["updated_at"], version)
        self.assertEqual(self.db[COLLECTION].find_one({"target_id": "repeat"}), decision)
        self.graph.nodes["Repository", "repeat"]["name"] = "Automatic name"
        apply_overrides(self.graph, self.db)
        self.assertEqual(self.graph.nodes["Repository", "repeat"]["name"], "001")

    def test_failed_decision_write_restores_original_legacy_type_and_relationships(self):
        for index, previous in enumerate((True, 0, ["old"], {"old": True})):
            with self.subTest(previous=previous):
                node_id = f"rollback-{index}"
                edge = self.add_repository(node_id, previous)
                with patch.object(nodes, "record_override", side_effect=AutoReconnect("write failed")):
                    response = self.edit(node_id, name="recovered")
                self.assertEqual(response.status_code, 503, response.text)
                row = self.graph.nodes["Repository", node_id]
                self.assertIs(type(row["name"]), type(previous))
                self.assertEqual(row["name"], previous)
                self.assertEqual(row["description"], "Keep description")
                self.assertEqual(self.graph.relationships[edge], {"source": "manual"})
                self.assertIsNone(self.db[COLLECTION].find_one({"target_id": node_id}))

    def test_stale_form_does_not_overwrite_a_more_recent_name(self):
        self.add_repository("stale", True)
        old = self.form("stale")
        self.assertEqual(self.edit("stale", name="newest").status_code, 303)
        old["fields"]["name"] = "outdated"
        response = self.client.post(old["action"], data=old["fields"])
        self.assertIn("stale=1", response.headers["location"])
        self.assertEqual(self.graph.nodes["Repository", "stale"]["name"], "newest")
        self.assertEqual(self.db[COLLECTION].find_one({"target_id": "stale"})["fields"], {"name": "newest"})


if __name__ == "__main__":
    unittest.main()
