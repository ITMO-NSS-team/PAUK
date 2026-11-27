"""Unit tests for agent link suggestion moderation routes (`/suggestions`)."""

import unittest
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable, SessionExpired
from pymongo.errors import AutoReconnect

from pauk.admin import deps
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user, session_key
from pauk.graph.overrides import COLLECTION as OVERRIDES
from pauk.graph.overrides import (
    CREATE,
    DELETE,
    LINK,
    SET,
    active_overrides,
    apply_overrides,
    record_relationship_override,
)
from pauk.settings import Settings
from pauk.storage import link_suggestions
from tests.unit.test_admin_nodes import FakePanelGraph


class AdminSuggestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = mongomock.MongoClient()["pauk_admin_suggestions_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "adminuser", "hunter2", role="admin")
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.graph = FakePanelGraph()
        self.graph.add("Publication", "W1", title="Paper One", doi="10.1234/w1", year=2025, pdf_urls=[])
        self.graph.add(
            "Publication",
            "W2",
            title="Paper Two",
            merged_ids=["W2_OLD"],
            pdf_urls=["https://example.org/already.pdf"],
        )
        self.graph.add("Repository", "r-existing", url="https://github.com/org/existing", name="existing")

        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_for] = lambda: self.graph
        app.dependency_overrides[deps.graph_if_up] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.addCleanup(self.client.close)

    def sign_in(self, login: str = "roman") -> str:
        self.client.post("/login", data={"login": login, "password": "hunter2"})
        token = self.client.cookies[COOKIE]
        return self.db[SESSIONS].find_one({"_id": session_key(token)})["csrf"]

    def test_auth_viewer_restrictions_csrf_and_dashboard_wiring(self) -> None:
        self.assertEqual(self.client.get("/suggestions").status_code, 401)
        s = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-existing",
            rationale="Found code reference in paper.",
        )

        guest_csrf = self.sign_in("guest")
        page = self.client.get("/suggestions")
        self.assertEqual(page.status_code, 200)
        self.assertIn("от агента (требует проверки)", page.text)
        self.assertNotIn("·", page.text)
        self.assertNotIn("/approve", page.text)
        self.assertEqual(
            self.client.post(f"/suggestions/{s['suggestion_id']}/approve", data={"csrf": guest_csrf}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(f"/suggestions/{s['suggestion_id']}/reject", data={"csrf": guest_csrf}).status_code,
            403,
        )

        self.sign_in("roman")
        self.assertEqual(
            self.client.post(f"/suggestions/{s['suggestion_id']}/approve", data={"csrf": "bad"}).status_code,
            403,
        )
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('href="/suggestions"', home.text)
        self.assertIn("Предложения по связям", home.text)

    def test_approve_existing_and_brand_new_implements_and_double_approve(self) -> None:
        csrf = self.sign_in()
        s_existing = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-existing",
            rationale="Author link confirmed.",
        )["suggestion_id"]
        r1 = self.client.post(
            f"/suggestions/{s_existing}/approve",
            data={"csrf": csrf, "note": "Looks valid", "tab": "pending"},
        )
        self.assertEqual(r1.status_code, 303)
        self.assertIn("done=approved", r1.headers["location"])
        self.assertIn(("Repository", "IMPLEMENTS", "Publication", "r-existing", "W1"), self.graph.relationships)
        rel_claims = self.db[OVERRIDES].find_one(
            {"kind": "rel", "op": LINK, "src_id": "r-existing", "target_id": "W1"}
        )
        self.assertIsNotNone(rel_claims)
        self.assertTrue(rel_claims["active"])

        r1_dup = self.client.post(f"/suggestions/{s_existing}/approve", data={"csrf": csrf})
        self.assertEqual(r1_dup.status_code, 303)
        self.assertIn("problem=", r1_dup.headers["location"])

        for i in range(25):
            self.graph.add("Repository", f"r-sub-{i:02d}", url=f"https://github.com/org/target-{i:02d}")
        self.graph.add("Repository", "r-sub-exact", url="https://github.com/org/target")
        admin_csrf = self.sign_in("adminuser")
        s_many = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/org/target",
            rationale="Found exact repo beyond 20 prefix matches.",
        )["suggestion_id"]
        r_many = self.client.post(f"/suggestions/{s_many}/approve", data={"csrf": admin_csrf})
        self.assertEqual(r_many.status_code, 303)
        self.assertIn("done=approved", r_many.headers["location"])
        self.assertIn(("Repository", "IMPLEMENTS", "Publication", "r-sub-exact", "W1"), self.graph.relationships)
        csrf = self.sign_in("roman")

        s_new = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/NewOrg/CoolTool",
            rationale="GitHub URL found in paper footnotes.",
        )["suggestion_id"]
        r2 = self.client.post(f"/suggestions/{s_new}/approve", data={"csrf": csrf})
        self.assertEqual(r2.status_code, 303)
        self.assertIn("done=approved", r2.headers["location"])
        self.assertIn(("Repository", "github_neworg_cooltool"), self.graph.nodes)
        self.assertIn(
            ("Repository", "IMPLEMENTS", "Publication", "github_neworg_cooltool", "W1"),
            self.graph.relationships,
        )
        self.assertIsNotNone(self.db[OVERRIDES].find_one({"_id": "node:Repository:github_neworg_cooltool", "op": CREATE}))

    def test_approve_not_implements_deletes_and_tombstones(self) -> None:
        csrf = self.sign_in()
        self.graph.relationships[("Repository", "IMPLEMENTS", "Publication", "r-existing", "W1")] = {}
        s_del = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.NOT_IMPLEMENTS,
            repository_id="r-existing",
            rationale="Third-party benchmark, not authors' code.",
        )["suggestion_id"]
        resp = self.client.post(f"/suggestions/{s_del}/approve", data={"csrf": csrf, "note": "Verified"})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("done=approved", resp.headers["location"])
        self.assertNotIn(("Repository", "IMPLEMENTS", "Publication", "r-existing", "W1"), self.graph.relationships)
        tombstone = self.db[OVERRIDES].find_one(
            {"kind": "rel", "op": DELETE, "src_id": "r-existing", "target_id": "W1", "active": True}
        )
        self.assertIsNotNone(tombstone)

        s_noop = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.NOT_IMPLEMENTS,
            repository_url="https://github.com/org/never-in-graph",
            rationale="Explicit negative record for absent repo.",
        )["suggestion_id"]
        resp_noop = self.client.post(f"/suggestions/{s_noop}/approve", data={"csrf": csrf})
        self.assertEqual(resp_noop.status_code, 303)
        self.assertIn("done=approved", resp_noop.headers["location"])

        self.graph.add("Repository", "r-tomb", url="https://github.com/org/tomb")
        record_relationship_override(
            self.db, "Repository", "IMPLEMENTS", "Publication", "r-tomb", "W1",
            op=DELETE, actor="user:roman", snapshot={"manual_prop": "keep-me"},
        )
        s_keep = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.NOT_IMPLEMENTS,
            repository_id="r-tomb",
            rationale="Confirming already unlinked relationship.",
        )["suggestion_id"]
        resp_keep = self.client.post(f"/suggestions/{s_keep}/approve", data={"csrf": csrf})
        self.assertEqual(resp_keep.status_code, 303)
        self.assertIn("done=approved", resp_keep.headers["location"])
        kept_ov = self.db[OVERRIDES].find_one(
            {"kind": "rel", "op": DELETE, "src_id": "r-tomb", "target_id": "W1"}
        )
        self.assertEqual(kept_ov["snapshot"], {"manual_prop": "keep-me"})

    def test_approve_mentions_existing_and_link_candidate(self) -> None:
        csrf = self.sign_in()
        s_repo = link_suggestions.submit(
            self.db,
            publication_id="W2_OLD",
            proposed_relation=link_suggestions.MENTIONS,
            repository_url="https://github.com/org/existing",
            rationale="Paper cites baseline repo in section 4.",
            quote="We compare against github.com/org/existing",
        )["suggestion_id"]
        r1 = self.client.post(f"/suggestions/{s_repo}/approve", data={"csrf": csrf})
        self.assertEqual(r1.status_code, 303)
        self.assertIn("done=approved", r1.headers["location"])
        edge_key = ("Publication", "MENTIONS_LINK", "Repository", "W2", "https://github.com/org/existing")
        self.assertIn(edge_key, self.graph.relationships)
        self.assertEqual(self.graph.relationships[edge_key]["context"], ["We compare against github.com/org/existing"])
        self.assertEqual(self.graph.relationships[edge_key]["page_number"], [0])

        s_cand = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.MENTIONS,
            repository_url="https://github.com/other/baseline",
            rationale="Baseline code from related work.",
        )["suggestion_id"]
        r2 = self.client.post(f"/suggestions/{s_cand}/approve", data={"csrf": csrf})
        self.assertEqual(r2.status_code, 303)
        self.assertIn("done=approved", r2.headers["location"])
        self.assertIn(("LinkCandidate", "https://github.com/other/baseline"), self.graph.nodes)
        self.assertIn(
            ("Publication", "MENTIONS_LINK", "LinkCandidate", "W1", "https://github.com/other/baseline"),
            self.graph.relationships,
        )
        self.assertIsNotNone(
            self.db[OVERRIDES].find_one({"_id": "node:LinkCandidate:https://github.com/other/baseline", "op": CREATE})
        )

        self.graph.add(
            "LinkCandidate",
            "https://github.com/other/preexisting",
            url="https://github.com/other/preexisting",
            host="github.com",
        )
        s_pre = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.MENTIONS,
            repository_url="https://github.com/other/preexisting",
            rationale="Existing LinkCandidate protection test.",
        )["suggestion_id"]
        r3 = self.client.post(f"/suggestions/{s_pre}/approve", data={"csrf": csrf})
        self.assertEqual(r3.status_code, 303)
        self.assertIn("done=approved", r3.headers["location"])
        self.assertIsNotNone(
            self.db[OVERRIDES].find_one(
                {"_id": "node:LinkCandidate:https://github.com/other/preexisting", "op": CREATE, "active": True}
            )
        )

    def test_approve_full_text_and_idempotency(self) -> None:
        csrf = self.sign_in()
        s_ft = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.FULL_TEXT,
            evidence_url="https://arxiv.org/pdf/2501.00001.pdf",
            rationale="Found open-access preprint PDF.",
        )["suggestion_id"]
        r1 = self.client.post(f"/suggestions/{s_ft}/approve", data={"csrf": csrf})
        self.assertEqual(r1.status_code, 303)
        self.assertIn("done=approved", r1.headers["location"])
        self.assertEqual(self.graph.nodes[("Publication", "W1")]["pdf_urls"], ["https://arxiv.org/pdf/2501.00001.pdf"])
        ov = self.db[OVERRIDES].find_one({"_id": "node:Publication:W1", "op": SET})
        self.assertIsNone((ov.get("fields") or {}).get("pdf_urls"))
        self.assertEqual(ov["append_fields"]["pdf_urls"], ["https://arxiv.org/pdf/2501.00001.pdf"])

        self.graph.nodes[("Publication", "W1")]["pdf_urls"] = ["https://example.org/pipeline-found.pdf"]
        apply_overrides(self.graph, self.db)
        self.assertEqual(
            self.graph.nodes[("Publication", "W1")]["pdf_urls"],
            ["https://example.org/pipeline-found.pdf", "https://arxiv.org/pdf/2501.00001.pdf"],
        )

        s_dup = link_suggestions.submit(
            self.db,
            publication_id="W2",
            proposed_relation=link_suggestions.FULL_TEXT,
            evidence_url="https://example.org/already.pdf",
            rationale="Duplicate URL check.",
        )["suggestion_id"]
        r2 = self.client.post(f"/suggestions/{s_dup}/approve", data={"csrf": csrf})
        self.assertEqual(r2.status_code, 303)
        self.assertEqual(self.graph.nodes[("Publication", "W2")]["pdf_urls"], ["https://example.org/already.pdf"])

    def test_reject_does_not_touch_graph_or_overrides(self) -> None:
        csrf = self.sign_in()
        nodes_before = dict(self.graph.nodes)
        s = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/org/rejected",
            rationale="Hallucinated link.",
        )["suggestion_id"]
        resp = self.client.post(f"/suggestions/{s}/reject", data={"csrf": csrf, "note": "Wrong paper"})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("done=rejected", resp.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s)["status"], link_suggestions.REJECTED)
        self.assertEqual(self.graph.nodes, nodes_before)
        self.assertEqual(self.graph.relationships, {})
        self.assertEqual(active_overrides(self.db), [])

    def test_error_handling_rollback_xss_and_tampered_url(self) -> None:
        csrf = self.sign_in()
        s_missing_pub = link_suggestions.submit(
            self.db,
            publication_id="W_MISSING",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-existing",
            rationale="Missing publication test.",
        )["suggestion_id"]
        resp_missing = self.client.post(f"/suggestions/{s_missing_pub}/approve", data={"csrf": csrf})
        self.assertEqual(resp_missing.status_code, 303)
        self.assertIn("problem=", resp_missing.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_missing_pub)["status"], link_suggestions.PENDING)

        self.graph.add("Repository", "r-dup1", url="https://github.com/dup/repo")
        self.graph.add("Repository", "r-dup2", cited_urls=["https://github.com/dup/repo/"])
        s_amb = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/dup/repo",
            rationale="Ambiguous repo match test.",
        )["suggestion_id"]
        resp_amb = self.client.post(f"/suggestions/{s_amb}/approve", data={"csrf": csrf})
        self.assertEqual(resp_amb.status_code, 303)
        self.assertIn("problem=", resp_amb.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_amb)["status"], link_suggestions.PENDING)

        s_rollback = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-existing",
            rationale="Mongo failure rollback check.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.record_relationship_override", side_effect=AutoReconnect("down")):
            resp_fail = self.client.post(f"/suggestions/{s_rollback}/approve", data={"csrf": csrf})
        self.assertEqual(resp_fail.status_code, 503)
        self.assertNotIn(("Repository", "IMPLEMENTS", "Publication", "r-existing", "W1"), self.graph.relationships)
        self.assertEqual(link_suggestions.get(self.db, s_rollback)["status"], link_suggestions.PENDING)

        link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.FULL_TEXT,
            evidence_url="https://example.org/doc.pdf",
            rationale="<script>alert('xss')</script> found in footer.",
            quote="<img src=x onerror=alert(1)>",
        )
        self.db[link_suggestions.COLLECTION].insert_one(
            {
                "_id": "sug_tampered_url",
                "dedup_key": "tampered",
                "content_hash": "tampered",
                "status": link_suggestions.PENDING,
                "publication_id": "W1",
                "proposed_relation": link_suggestions.MENTIONS,
                "repository_url": "javascript:alert(1)",
                "evidence_url": "javascript:alert(2)",
                "rationale": "Tampered DB URL check.",
                "submitted_by": "mcp",
                "submitted_at": link_suggestions._now(),
                "submission_count": 1,
            }
        )
        rendered = self.client.get("/suggestions").text
        self.assertNotIn("<script>alert('xss')</script>", rendered)
        self.assertIn("&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;", rendered)
        self.assertNotIn('href="javascript:', rendered)

    def test_graph_unavailable_renders_pending_page_gracefully(self) -> None:
        self.sign_in()
        link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-existing",
            rationale="Queue opens when graph is down.",
        )
        with patch.object(self.graph, "fetch_node_properties", side_effect=ServiceUnavailable("down")):
            page = self.client.get("/suggestions")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Граф не отвечает", page.text)

    def test_two_step_compensation_version_conflict_neo4j_error_and_expired_claim(self) -> None:
        csrf = self.sign_in()

        s_step2_impl = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/step2/rollback-impl",
            rationale="Two-step rollback for implements.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.record_relationship_override", side_effect=AutoReconnect("down")):
            resp_s2 = self.client.post(f"/suggestions/{s_step2_impl}/approve", data={"csrf": csrf})
        self.assertEqual(resp_s2.status_code, 503)
        self.assertNotIn(("Repository", "github_step2_rollback-impl"), self.graph.nodes)
        self.assertIsNone(
            self.db[OVERRIDES].find_one({"_id": "node:Repository:github_step2_rollback-impl", "active": True})
        )
        self.assertEqual(link_suggestions.get(self.db, s_step2_impl)["status"], link_suggestions.PENDING)

        s_step2_ment = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.MENTIONS,
            repository_url="https://github.com/step2/rollback-ment",
            rationale="Two-step rollback for mentions.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.record_relationship_override", side_effect=AutoReconnect("down")):
            resp_m2 = self.client.post(f"/suggestions/{s_step2_ment}/approve", data={"csrf": csrf})
        self.assertEqual(resp_m2.status_code, 503)
        self.assertNotIn(("LinkCandidate", "https://github.com/step2/rollback-ment"), self.graph.nodes)
        self.assertIsNone(
            self.db[OVERRIDES].find_one(
                {"_id": "node:LinkCandidate:https://github.com/step2/rollback-ment", "active": True}
            )
        )
        self.assertEqual(link_suggestions.get(self.db, s_step2_ment)["status"], link_suggestions.PENDING)

        s_ft_vc = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.FULL_TEXT,
            evidence_url="https://arxiv.org/pdf/2501.99999.pdf",
            rationale="Concurrent update conflict test.",
        )["suggestion_id"]
        with patch.object(
            self.graph,
            "update_node_properties",
            side_effect=lambda *args, **kwargs: (self.graph.nodes[("Publication", "W1")], None),
        ):
            resp_vc = self.client.post(f"/suggestions/{s_ft_vc}/approve", data={"csrf": csrf})
        self.assertEqual(resp_vc.status_code, 303)
        self.assertIn("problem=", resp_vc.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_ft_vc)["status"], link_suggestions.PENDING)

        s_neo_down = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-existing",
            rationale="Neo4j unavailable during approve.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes._resolve_publication", side_effect=ServiceUnavailable("down")):
            resp_down = self.client.post(f"/suggestions/{s_neo_down}/approve", data={"csrf": csrf})
        self.assertEqual(resp_down.status_code, 503)
        self.assertEqual(link_suggestions.get(self.db, s_neo_down)["status"], link_suggestions.PENDING)

        s_expired = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-existing",
            rationale="Lease expiration on complete_approval.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.link_suggestions.complete_approval", return_value=None):
            resp_exp = self.client.post(f"/suggestions/{s_expired}/approve", data={"csrf": csrf})
        self.assertEqual(resp_exp.status_code, 303)
        self.assertIn("problem=", resp_exp.headers["location"])
        self.assertNotIn("done=approved", resp_exp.headers["location"])

        self.graph.add(
            "LinkCandidate",
            "https://github.com/step2/preexisting-cand",
            url="https://github.com/step2/preexisting-cand",
            host="github.com",
        )
        s_pre_fail = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.MENTIONS,
            repository_url="https://github.com/step2/preexisting-cand",
            rationale="Pre-existing candidate rollback test.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.record_relationship_override", side_effect=AutoReconnect("down")):
            resp_pre_fail = self.client.post(f"/suggestions/{s_pre_fail}/approve", data={"csrf": csrf})
        self.assertEqual(resp_pre_fail.status_code, 503)
        self.assertIsNone(
            self.db[OVERRIDES].find_one(
                {"_id": "node:LinkCandidate:https://github.com/step2/preexisting-cand", "active": True}
            )
        )
        self.assertEqual(link_suggestions.get(self.db, s_pre_fail)["status"], link_suggestions.PENDING)

        self.graph.add("Repository", "r-sess", url="https://github.com/org/session-expired")
        s_sess = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-sess",
            rationale="SessionExpired handling test.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes._resolve_publication", side_effect=SessionExpired("expired")):
            resp_sess = self.client.post(f"/suggestions/{s_sess}/approve", data={"csrf": csrf})
        self.assertEqual(resp_sess.status_code, 503)
        self.assertEqual(link_suggestions.get(self.db, s_sess)["status"], link_suggestions.PENDING)

        s_mongo_done = link_suggestions.submit(
            self.db,
            publication_id="W2",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r-sess",
            rationale="Mongo failure on complete_approval.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.link_suggestions.complete_approval", side_effect=AutoReconnect("down")):
            resp_mongo_done = self.client.post(f"/suggestions/{s_mongo_done}/approve", data={"csrf": csrf})
        self.assertEqual(resp_mongo_done.status_code, 503)

    def test_malformed_suggestion_documents_render_gracefully(self) -> None:
        self.sign_in()
        self.db[link_suggestions.COLLECTION].insert_one(
            {
                "_id": "sug_malformed_doc",
                "status": link_suggestions.PENDING,
                "submission_count": "not-an-int",
                "submitted_at": "2026-11-25T10:00:00Z",
                "publication_id": 123,
                "proposed_relation": None,
            }
        )
        resp = self.client.get("/suggestions")
        self.assertEqual(resp.status_code, 200)

    def test_suggestions_templates_have_no_interpuncts_and_sanitize_applying_by(self) -> None:
        csrf = self.sign_in()
        s_reopen = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/ui/no-dots",
            rationale="First rejection for prior display check.",
        )["suggestion_id"]
        self.client.post(f"/suggestions/{s_reopen}/reject", data={"csrf": csrf, "note": "first"})
        link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/ui/no-dots",
            rationale="Reopened with new rationale for prior banner.",
        )
        for tab in ("pending", "rejected"):
            self.assertNotIn("·", self.client.get(f"/suggestions?tab={tab}").text)

        self.db[link_suggestions.COLLECTION].insert_one(
            {
                "_id": "sug_missing_applying_by",
                "dedup_key": "k_missing_applying_by",
                "content_hash": "c_missing_applying_by",
                "status": link_suggestions.APPLYING,
                "active": True,
                "needs_recovery": True,
                "recovery_reason": "Earlier probe warning",
                "publication_id": "W1",
                "proposed_relation": link_suggestions.IMPLEMENTS,
                "repository_id": "r-existing",
                "rationale": "Missing applying_by display check.",
                "submitted_by": "mcp",
                "submitted_at": link_suggestions._now(),
                "submission_count": 1,
            }
        )
        page_html = self.client.get("/suggestions").text
        self.assertIn("обработка зависла (?)", page_html)
        self.assertNotIn("(None)", page_html)
        self.assertIn("Вернуть в ожидание", page_html)

    def test_implements_driver_error_probing_and_compensation(self) -> None:
        from pauk.admin import suggestion_routes

        csrf = self.sign_in()

        s_probe_err = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/err/edge-exists-down",
            rationale="Failure right after node creation.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes._edge_exists", side_effect=ServiceUnavailable("down")):
            resp = self.client.post(f"/suggestions/{s_probe_err}/approve", data={"csrf": csrf})
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn(("Repository", "github_err_edge-exists-down"), self.graph.nodes)
        self.assertIsNone(
            self.db[OVERRIDES].find_one({"_id": "node:Repository:github_err_edge-exists-down", "active": True})
        )
        self.assertEqual(link_suggestions.get(self.db, s_probe_err)["status"], link_suggestions.PENDING)

        s_create_err = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/err/create-edge-down",
            rationale="Confirmed non-write during DriverError.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.create_relationship", side_effect=ServiceUnavailable("timeout")):
            resp = self.client.post(f"/suggestions/{s_create_err}/approve", data={"csrf": csrf})
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn(("Repository", "github_err_create-edge-down"), self.graph.nodes)
        self.assertIsNone(
            self.db[OVERRIDES].find_one({"_id": "node:Repository:github_err_create-edge-down", "active": True})
        )
        self.assertEqual(link_suggestions.get(self.db, s_create_err)["status"], link_suggestions.PENDING)

        s_committed_err = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/err/commit-lost-reply",
            rationale="Edge committed despite lost DriverError reply.",
        )["suggestion_id"]
        orig_create_rel = suggestion_routes.create_relationship

        def commit_then_drop(*args, **kwargs):
            orig_create_rel(*args, **kwargs)
            raise SessionExpired("reply lost")

        with patch("pauk.admin.suggestion_routes.create_relationship", side_effect=commit_then_drop):
            resp = self.client.post(f"/suggestions/{s_committed_err}/approve", data={"csrf": csrf})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("done=approved", resp.headers["location"])
        self.assertIn(("Repository", "github_err_commit-lost-reply"), self.graph.nodes)
        self.assertIn(
            ("Repository", "IMPLEMENTS", "Publication", "github_err_commit-lost-reply", "W1"),
            self.graph.relationships,
        )
        self.assertIsNotNone(
            self.db[OVERRIDES].find_one(
                {
                    "kind": "rel",
                    "op": LINK,
                    "src_id": "github_err_commit-lost-reply",
                    "target_id": "W1",
                    "active": True,
                }
            )
        )

        s_uncertain = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/err/uncertain-outcome",
            rationale="Both write and probe drop connection.",
        )["suggestion_id"]
        orig_edge_exists = suggestion_routes._edge_exists
        fetch_calls = {"n": 0}

        def drop_on_second_fetch(*args, **kwargs):
            fetch_calls["n"] += 1
            if fetch_calls["n"] == 1:
                return orig_edge_exists(*args, **kwargs)
            raise ServiceUnavailable("probe failed too")

        with (
            patch("pauk.admin.suggestion_routes.create_relationship", side_effect=commit_then_drop),
            patch("pauk.admin.suggestion_routes._edge_exists", side_effect=drop_on_second_fetch),
        ):
            resp = self.client.post(f"/suggestions/{s_uncertain}/approve", data={"csrf": csrf})
        self.assertEqual(resp.status_code, 503)
        stuck_doc = link_suggestions.get(self.db, s_uncertain)
        self.assertEqual(stuck_doc["status"], link_suggestions.APPLYING)
        self.assertTrue(stuck_doc["needs_recovery"])
        reject_stuck = self.client.post(f"/suggestions/{s_uncertain}/reject", data={"csrf": csrf})
        self.assertEqual(reject_stuck.status_code, 303)
        self.assertIn("problem=", reject_stuck.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_uncertain)["status"], link_suggestions.APPLYING)
        rec_partial = self.client.post(f"/suggestions/{s_uncertain}/recover", data={"csrf": csrf})
        self.assertEqual(rec_partial.status_code, 303)
        self.assertIn("problem=", rec_partial.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_uncertain)["status"], link_suggestions.APPLYING)

    def test_create_node_driver_error_probing_and_recovery(self) -> None:
        from pauk.admin import suggestion_routes

        csrf = self.sign_in()

        s_cn_absent = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/cn/absent",
            rationale="create_node absent on DriverError.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.create_node", side_effect=ServiceUnavailable("down")):
            r_abs = self.client.post(f"/suggestions/{s_cn_absent}/approve", data={"csrf": csrf})
        self.assertEqual(r_abs.status_code, 503)
        self.assertEqual(link_suggestions.get(self.db, s_cn_absent)["status"], link_suggestions.PENDING)

        s_cn_commit = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/cn/committed",
            rationale="create_node committed despite DriverError.",
        )["suggestion_id"]
        orig_create_node = suggestion_routes.create_node

        def create_node_then_drop(*args, **kwargs):
            orig_create_node(*args, **kwargs)
            raise SessionExpired("lost reply")

        with patch("pauk.admin.suggestion_routes.create_node", side_effect=create_node_then_drop):
            r_com = self.client.post(f"/suggestions/{s_cn_commit}/approve", data={"csrf": csrf})
        self.assertEqual(r_com.status_code, 303)
        self.assertIn("done=approved", r_com.headers["location"])
        self.assertIsNotNone(self.db[OVERRIDES].find_one({"_id": "node:Repository:github_cn_committed", "op": CREATE}))

        s_cn_uncertain = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/cn/uncertain",
            rationale="create_node and probe both fail.",
        )["suggestion_id"]
        orig_fetch = self.graph.fetch_node_properties
        probe_calls = {"n": 0}

        def fail_on_cn_probe(label, node_id):
            if label == "Repository" and node_id == "github_cn_uncertain":
                probe_calls["n"] += 1
                if probe_calls["n"] >= 2:
                    raise ServiceUnavailable("probe down")
            return orig_fetch(label, node_id)

        with (
            patch("pauk.admin.suggestion_routes.create_node", side_effect=ServiceUnavailable("drop")),
            patch.object(self.graph, "fetch_node_properties", side_effect=fail_on_cn_probe),
        ):
            r_unc = self.client.post(f"/suggestions/{s_cn_uncertain}/approve", data={"csrf": csrf})
        self.assertEqual(r_unc.status_code, 503)
        unc_doc = link_suggestions.get(self.db, s_cn_uncertain)
        self.assertEqual(unc_doc["status"], link_suggestions.APPLYING)
        self.assertTrue(unc_doc["needs_recovery"])

    def test_undo_failure_during_record_marks_needs_recovery(self) -> None:
        csrf = self.sign_in()

        s_undo_drv = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/undo/driver-err",
            rationale="undo fails with DriverError.",
        )["suggestion_id"]
        with (
            patch("pauk.admin.suggestion_routes.record_relationship_override", side_effect=AutoReconnect("mongo")),
            patch("pauk.admin.suggestion_routes.delete_relationship", side_effect=ServiceUnavailable("neo4j")),
        ):
            self.assertEqual(self.client.post(f"/suggestions/{s_undo_drv}/approve", data={"csrf": csrf}).status_code, 503)
        self.assertEqual(link_suggestions.get(self.db, s_undo_drv)["status"], link_suggestions.APPLYING)
        self.assertTrue(link_suggestions.get(self.db, s_undo_drv)["needs_recovery"])

        s_undo_false = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/undo/comp-false",
            rationale="compensation returns False during _record rollback.",
        )["suggestion_id"]
        with (
            patch("pauk.admin.suggestion_routes.record_relationship_override", side_effect=AutoReconnect("mongo")),
            patch("pauk.admin.suggestion_routes._compensate_created_node", return_value=False),
        ):
            self.assertEqual(
                self.client.post(f"/suggestions/{s_undo_false}/approve", data={"csrf": csrf}).status_code,
                503,
            )
        self.assertEqual(link_suggestions.get(self.db, s_undo_false)["status"], link_suggestions.APPLYING)
        self.assertTrue(link_suggestions.get(self.db, s_undo_false)["needs_recovery"])

    def test_recover_reconciles_applied_unapplied_partial_and_force_reset(self) -> None:
        from datetime import timedelta

        csrf = self.sign_in()

        s_rec_ok = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/err/complete-approval-down",
            rationale="Recovery completes committed approval.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.link_suggestions.complete_approval", side_effect=AutoReconnect("down")):
            self.assertEqual(
                self.client.post(f"/suggestions/{s_rec_ok}/approve", data={"csrf": csrf}).status_code,
                503,
            )
        rec_page = self.client.get("/suggestions").text
        self.assertIn(f"/suggestions/{s_rec_ok}/recover", rec_page)
        self.assertIn("Проверить и восстановить", rec_page)
        rec_ok_resp = self.client.post(f"/suggestions/{s_rec_ok}/recover", data={"csrf": csrf})
        self.assertEqual(rec_ok_resp.status_code, 303)
        self.assertIn("done=recovered_approved", rec_ok_resp.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_rec_ok)["status"], link_suggestions.APPROVED)
        self.assertFalse(link_suggestions.get(self.db, s_rec_ok)["active"])

        s_rec_none = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/err/never-wrote",
            rationale="Lease expired before any graph write.",
        )["suggestion_id"]
        claimed_none = link_suggestions.claim_for_apply(self.db, s_rec_none, "user:crashed")
        self.db[link_suggestions.COLLECTION].update_one(
            {"_id": s_rec_none},
            {
                "$set": {
                    "applying_at": claimed_none["applying_at"]
                    - timedelta(seconds=link_suggestions.APPLYING_LEASE_SECONDS + 10)
                }
            },
        )
        self.assertIn("problem=", self.client.post(f"/suggestions/{s_rec_none}/reject", data={"csrf": csrf}).headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_rec_none)["status"], link_suggestions.APPLYING)
        rec_none_resp = self.client.post(f"/suggestions/{s_rec_none}/recover", data={"csrf": csrf})
        self.assertIn("done=recovered_pending", rec_none_resp.headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_rec_none)["status"], link_suggestions.PENDING)
        self.assertIn("done=rejected", self.client.post(f"/suggestions/{s_rec_none}/reject", data={"csrf": csrf}).headers["location"])
        self.assertEqual(link_suggestions.get(self.db, s_rec_none)["status"], link_suggestions.REJECTED)

        s_partial = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/err/force-reset",
            rationale="Partial diagnosis permits explicit force reset.",
        )["suggestion_id"]
        partial_claim = link_suggestions.claim_for_apply(self.db, s_partial, "user:roman")
        link_suggestions.mark_needs_recovery(
            self.db, s_partial, partial_claim["apply_token"], reason="Verified partial edge manually"
        )
        self.assertIn(
            "done=recovered_pending",
            self.client.post(
                f"/suggestions/{s_partial}/recover",
                data={"csrf": csrf, "force_reset": "1"},
            ).headers["location"],
        )
        self.assertEqual(link_suggestions.get(self.db, s_partial)["status"], link_suggestions.PENDING)

    def test_recover_auth_csrf_errors_and_relation_types(self) -> None:
        from pauk.graph.overrides import record_override

        csrf = self.sign_in()
        self.assertEqual(self.client.post("/suggestions/sug_missing/recover", data={"csrf": csrf}).status_code, 404)
        self.assertEqual(self.client.post("/suggestions/sug_missing/recover", data={"csrf": "bad"}).status_code, 403)
        guest_csrf = self.sign_in("guest")
        self.assertEqual(self.client.post("/suggestions/sug_missing/recover", data={"csrf": guest_csrf}).status_code, 403)
        csrf = self.sign_in("roman")

        cand_url = "https://github.com/other/set-candidate"
        self.graph.add("LinkCandidate", cand_url, url=cand_url, host="github.com")
        record_override(self.db, "LinkCandidate", cand_url, SET, {"host": "github.com"}, actor="user:roman")
        s_ment_rec = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.MENTIONS,
            repository_url=cand_url,
            rationale="LinkCandidate with SET override recovery.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.link_suggestions.complete_approval", side_effect=AutoReconnect("down")):
            self.assertEqual(self.client.post(f"/suggestions/{s_ment_rec}/approve", data={"csrf": csrf}).status_code, 503)
        self.assertIn(
            "done=recovered_approved",
            self.client.post(f"/suggestions/{s_ment_rec}/recover", data={"csrf": csrf}).headers["location"],
        )

        self.graph.relationships[("Repository", "IMPLEMENTS", "Publication", "r-existing", "W1")] = {}
        s_ni_rec = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.NOT_IMPLEMENTS,
            repository_id="r-existing",
            rationale="NOT_IMPLEMENTS recovery.",
        )["suggestion_id"]
        with patch("pauk.admin.suggestion_routes.link_suggestions.complete_approval", side_effect=AutoReconnect("down")):
            self.assertEqual(self.client.post(f"/suggestions/{s_ni_rec}/approve", data={"csrf": csrf}).status_code, 503)
        self.assertIn(
            "done=recovered_approved",
            self.client.post(f"/suggestions/{s_ni_rec}/recover", data={"csrf": csrf}).headers["location"],
        )

        s_ft_rec = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.FULL_TEXT,
            evidence_url="https://arxiv.org/pdf/2501.55555.pdf",
            rationale="FULL_TEXT recovery and failure reason preservation.",
        )["suggestion_id"]
        ft_claim = link_suggestions.claim_for_apply(self.db, s_ft_rec, "user:roman")
        link_suggestions.mark_needs_recovery(self.db, s_ft_rec, ft_claim["apply_token"], reason="Keep my reason")
        with patch("pauk.admin.suggestion_routes._inspect_applied_state", side_effect=ServiceUnavailable("neo down")):
            self.assertEqual(self.client.post(f"/suggestions/{s_ft_rec}/recover", data={"csrf": csrf}).status_code, 503)
        kept = link_suggestions.get(self.db, s_ft_rec)
        self.assertTrue(kept["needs_recovery"])
        self.assertEqual(kept["recovery_reason"], "Keep my reason")
        self.assertIn(
            "done=recovered_pending",
            self.client.post(f"/suggestions/{s_ft_rec}/recover", data={"csrf": csrf}).headers["location"],
        )
        self.assertEqual(link_suggestions.get(self.db, s_ft_rec)["status"], link_suggestions.PENDING)

    def test_full_text_already_present_preserves_graph_version(self) -> None:
        from neo4j.time import DateTime

        from pauk.graph.overrides import override_id, record_override

        csrf = self.sign_in()
        self.graph.nodes[("Publication", "W2")]["updated_at"] = DateTime(2026, 11, 27, 9, 0, 0)
        record_override(
            self.db,
            "Publication",
            "W2",
            SET,
            {"title": "Paper Two"},
            actor="user:roman",
            graph_updated_at=DateTime(2026, 11, 27, 9, 0, 0),
        )
        s_ft_keep = link_suggestions.submit(
            self.db,
            publication_id="W2",
            proposed_relation=link_suggestions.FULL_TEXT,
            evidence_url="https://example.org/already.pdf",
            rationale="Preserve existing graph_version.",
        )["suggestion_id"]
        self.assertIn(
            "done=approved",
            self.client.post(f"/suggestions/{s_ft_keep}/approve", data={"csrf": csrf}).headers["location"],
        )
        w2_ov = self.db[OVERRIDES].find_one({"_id": override_id("Publication", "W2")})
        self.assertIn("graph_version", w2_ov)
