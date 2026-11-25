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
from pauk.graph.overrides import CREATE, DELETE, LINK, SET, active_overrides, record_relationship_override
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
        self.assertIn("от агента · требует проверки", page.text)
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

        # Re-submitting approve on an already-approved suggestion does nothing.
        r1_dup = self.client.post(f"/suggestions/{s_existing}/approve", data={"csrf": csrf})
        self.assertEqual(r1_dup.status_code, 303)
        self.assertIn("problem=", r1_dup.headers["location"])

        # Admin role can also approve suggestions and >20 substring matches still find exact URL repo.
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

        # Brand-new GitHub repo creates Repository node + CREATE override + IMPLEMENTS edge + LINK claim.
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

        # Absent repo is an idempotent noop approval without creating a node.
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

        # Existing DELETE snapshot from an earlier unlink is preserved when tombstoning absent edge.
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

        # Pre-existing pipeline LinkCandidate also gets a protecting CREATE claim so prune keeps it.
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
        self.assertEqual(ov["fields"]["pdf_urls"], ["https://arxiv.org/pdf/2501.00001.pdf"])

        # Already present URL is an idempotent noop without duplicating pdf_urls.
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

        # Ambiguous URL in graph releases claim and refuses mutation.
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

        # Mongo override write failure rolls back graph and releases claim.
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

        # Untrusted text and tampered URL are safely neutralized when rendering /suggestions.
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

        # 2-step failure in _apply_implements (Mongo LINK override fails -> cleans up Step 1 Repository + CREATE).
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

        # 2-step failure in _apply_mentions (Mongo LINK override fails -> cleans up Step 1 LinkCandidate + CREATE).
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

        # VersionConflict on full_text releases claim and redirects cleanly.
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

        # ServiceUnavailable during approve releases claim and returns 503.
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

        # Expired claim token at complete_approval reports problem rather than done=approved.
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

        # Pre-existing pipeline LinkCandidate does NOT leak a newly created CREATE claim on Step 2 failure.
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

        # SessionExpired (a DriverError, not ServiceUnavailable) releases claim and returns 503.
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

        # PyMongoError during complete_approval returns clean 503 rather than 500.
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
