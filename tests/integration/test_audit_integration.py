"""Integration tests for AuditedNeo4jClient against a real Neo4j instance.

tests/unit/test_audit.py proves the diffing logic in isolation by mocking `_fetch_node_props`/`_fetch_rel_props` —
that's the right tool for the diff math itself, but it can't catch bugs in the Cypher those methods actually run
(the composite-label snapshot bug this file's test_label_growth_on_a_constrained_id_fails_atomically_no_audit_entry
regression-tests was exactly that kind of bug: invisible to a mock, only visible against a real database). These
tests spin up a disposable Neo4j container instead and exercise AuditedNeo4jClient's write path end to end.

Neither Docker nor `testcontainers` is a project dependency (see AGENTS.md — pytest itself isn't either, `uv sync`
stays minimal). Run explicitly, with Docker running:

    uv run --with pytest --with 'testcontainers[neo4j]' python -m pytest tests/integration -q

If `testcontainers` isn't installed or Docker isn't reachable, the whole module is skipped via `setUpModule`
rather than erroring at collection — so the CI command `uv run --with pytest pytest tests/ -q` still passes
untouched, it just shows this module's tests as skipped instead of running them.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from neo4j.exceptions import ConstraintError

from pauk.graph.audit import AuditedNeo4jClient, JSONLAuditSink, actor_context
from pauk.graph.client import Neo4jClient
from pauk.graph.schema import create_constraints

try:
    from testcontainers.community.neo4j import Neo4jContainer

    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only when the extra isn't installed
    Neo4jContainer = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR = exc

_PASSWORD = "integration-test-password"

_container = None
_raw_client: Neo4jClient | None = None


def setUpModule():  # noqa: N802 - unittest's required hook name
    global _container, _raw_client
    if _IMPORT_ERROR is not None:
        raise unittest.SkipTest(
            f"testcontainers not installed ({_IMPORT_ERROR}); "
            "run with --with 'testcontainers[neo4j]' to include these tests"
        )
    try:
        _container = Neo4jContainer(image="neo4j:5-community", password=_PASSWORD)
        _container.start()
    except Exception as exc:  # Docker daemon not running, no permission, image pull failure, etc.
        raise unittest.SkipTest(f"could not start a Neo4j container: {exc}") from exc
    _raw_client = Neo4jClient(_container.get_connection_url(), _container.username, _PASSWORD)
    create_constraints(_raw_client)


def tearDownModule():  # noqa: N802 - unittest's required hook name
    if _raw_client is not None:
        _raw_client.close()
    if _container is not None:
        _container.stop()


class InMemorySink:
    def __init__(self):
        self.entries = []

    def write(self, entries):
        self.entries.extend(entries)


class Neo4jIntegrationTestCase(unittest.TestCase):
    """Shared plumbing: one container for the whole module, a clean graph per test."""

    def setUp(self):
        with _raw_client.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    @staticmethod
    def _audited(diff_threshold: int = 50) -> tuple[AuditedNeo4jClient, InMemorySink]:
        sink = InMemorySink()
        return AuditedNeo4jClient(_raw_client, sink, diff_threshold=diff_threshold), sink

    @staticmethod
    def _node_props(node_id: str) -> dict | None:
        with _raw_client.driver.session() as session:
            record = session.run("MATCH (n {id: $id}) RETURN properties(n) AS props", id=node_id).single()
        return record["props"] if record else None

    @staticmethod
    def _node_labels(node_id: str) -> list[str] | None:
        with _raw_client.driver.session() as session:
            record = session.run("MATCH (n {id: $id}) RETURN labels(n) AS labels", id=node_id).single()
        return record["labels"] if record else None


class UpsertNodesIntegrationTest(Neo4jIntegrationTestCase):
    def test_create_then_update_then_noop_against_real_graph(self):
        audited, sink = self._audited()
        with actor_context("integration-test"):
            audited.upsert_nodes_batch("Department", [("d1", {"name": "CS"})])
            audited.upsert_nodes_batch("Department", [("d1", {"name": "Computer Science"})])
            audited.upsert_nodes_batch("Department", [("d1", {"name": "Computer Science"})])  # no-op

        self.assertEqual(len(sink.entries), 2)
        created, updated = sink.entries
        self.assertEqual(created.change_kind, "created")
        # "id" rides along in the diff too: properties(n) returns it like any other stored property, and
        # TECHNICAL_DIFF_FIELDS only excludes created_at/updated_at — a real-database finding the unit tests'
        # FakeNeo4jClient can't produce, since its mocked _fetch_node_props never puts "id" in the props dict.
        self.assertEqual(created.diff, {"id": (None, "d1"), "name": (None, "CS")})
        self.assertEqual(updated.change_kind, "updated")
        self.assertEqual(updated.diff, {"name": ("CS", "Computer Science")})
        self.assertEqual(self._node_props("d1")["name"], "Computer Science")

    def test_technical_fields_never_leak_into_the_diff(self):
        """created_at/updated_at are real neo4j.time.DateTime values on the node — the exclusion in
        _diff_props is deliberate scoping of what counts as a change, not an accident of the fields being unset."""
        audited, sink = self._audited()
        with actor_context("integration-test"):
            audited.upsert_nodes_batch("Department", [("d1", {"name": "CS"})])

        entry = sink.entries[0]
        self.assertNotIn("created_at", entry.diff)
        self.assertNotIn("updated_at", entry.diff)
        props = self._node_props("d1")
        self.assertIsNotNone(props["created_at"])
        self.assertIsNotNone(props["updated_at"])

    def test_composite_label_steady_state_diffs_correctly(self):
        """A node created and always addressed with the same label pair — the OR-match in _fetch_node_props
        must still find exactly the right row and not confuse it with an unrelated single-labeled Person."""
        _raw_client.upsert_nodes_batch("Person", [("other", {"email": "other@x.com"})])  # unrelated, no :Itmo

        audited, sink = self._audited()
        with actor_context("integration-test"):
            audited.upsert_nodes_batch(["Person", "Itmo"], [("p1", {"email": "a@x.com"})])
            audited.upsert_nodes_batch(["Person", "Itmo"], [("p1", {"email": "b@x.com"})])

        created, updated = sink.entries
        self.assertEqual(created.diff, {"id": (None, "p1"), "email": (None, "a@x.com")})
        self.assertEqual(updated.diff, {"email": ("a@x.com", "b@x.com")})
        # only p1 shows up in the audit trail, "other" was never touched by these calls
        self.assertEqual({e.entity_id for e in sink.entries}, {"p1"})
        self.assertEqual(self._node_props("other")["email"], "other@x.com")

    def test_label_growth_on_a_constrained_id_fails_atomically_no_audit_entry(self):
        """Regression test for the reviewer's bug report, verified against real Neo4j rather than a mock.

        Every label used in this schema carries an `id` uniqueness constraint (schema.py::CONSTRAINTS), so
        `MERGE (n:Person:Itmo {id: X})` against an id that already exists as plain `:Person` does not find
        that node — Cypher only matches on the *exact* label set — and tries to CREATE a second node with the
        same id, which the constraint rejects. So the label-growth scenario the _fetch_node_props fix guards
        against never actually reaches the diffing code today: it dies inside Neo4jClient.upsert_nodes_batch's
        own MERGE, before the "after" snapshot is even taken. What this test confirms is the other half of the
        contract that matters here: that failure still produces zero audit entries (never a bogus "created"
        record for a write that didn't happen), against a real driver exception rather than a fake RuntimeError.
        """
        _raw_client.upsert_nodes_batch("Person", [("p1", {"email": "a@x.com"})])
        audited, sink = self._audited()

        with self.assertRaises(ConstraintError), actor_context("integration-test"):
            audited.upsert_nodes_batch(["Person", "Itmo"], [("p1", {"email": "b@x.com"})])

        self.assertEqual(sink.entries, [])
        self.assertEqual(self._node_props("p1")["email"], "a@x.com")
        self.assertEqual(self._node_labels("p1"), ["Person"])

    def test_large_batch_writes_real_data_but_only_emits_bulk_summary(self):
        audited, sink = self._audited(diff_threshold=2)
        nodes = [("d1", {"name": "A"}), ("d2", {"name": "B"}), ("d3", {"name": "C"})]
        with actor_context("integration-test"):
            audited.upsert_nodes_batch("Department", nodes)

        self.assertEqual(len(sink.entries), 1)
        self.assertEqual(sink.entries[0].change_kind, "bulk")
        self.assertIn("3", sink.entries[0].entity_id)
        # the diffing shortcut doesn't skip the actual write
        self.assertEqual(self._node_props("d1")["name"], "A")
        self.assertEqual(self._node_props("d2")["name"], "B")
        self.assertEqual(self._node_props("d3")["name"], "C")


class UpsertRelationshipsIntegrationTest(Neo4jIntegrationTestCase):
    def test_relationship_created_then_updated(self):
        _raw_client.upsert_nodes_batch("Person", [("p1", {})])
        _raw_client.upsert_nodes_batch("Publication", [("pub1", {})])
        audited, sink = self._audited()

        with actor_context("integration-test"):
            matched = audited.upsert_relationships_batch(
                "Person", "Publication", "AUTHORED", [("p1", "pub1", {"position": 1})]
            )
            audited.upsert_relationships_batch("Person", "Publication", "AUTHORED", [("p1", "pub1", {"position": 2})])

        self.assertEqual(matched, 1)
        created, updated = sink.entries
        self.assertEqual(created.entity_type, "(Person)-[:AUTHORED]->(Publication)")
        self.assertEqual(created.entity_id, "p1 -> pub1")
        self.assertEqual(created.diff, {"position": (None, 1)})
        self.assertEqual(updated.diff, {"position": (1, 2)})

    def test_missing_target_matches_nothing_and_audits_nothing(self):
        _raw_client.upsert_nodes_batch("Person", [("p1", {})])
        audited, sink = self._audited()

        with actor_context("integration-test"):
            matched = audited.upsert_relationships_batch(
                "Person", "Publication", "AUTHORED", [("p1", "does-not-exist", {"position": 1})]
            )

        self.assertEqual(matched, 0)
        self.assertEqual(sink.entries, [])


class MergeNodesIntegrationTest(Neo4jIntegrationTestCase):
    def test_merge_person_nodes_batch_against_real_graph(self):
        _raw_client.upsert_nodes_batch("Person", [("dup", {"email": "dup@x.com", "orcid": "0000-1"})])
        _raw_client.upsert_nodes_batch("Person", [("canon", {"email": "canon@x.com"})])
        audited, sink = self._audited()

        with actor_context("integration-test"):
            removed = audited.merge_person_nodes_batch([("dup", "canon")])

        self.assertEqual(removed, 1)
        by_id = {e.entity_id: e for e in sink.entries}
        self.assertEqual(by_id["dup"].change_kind, "deleted")
        self.assertEqual(by_id["canon"].change_kind, "updated")
        self.assertEqual(by_id["canon"].diff, {"orcid": (None, "0000-1")})
        self.assertIsNone(self._node_props("dup"))  # duplicate is actually gone from the graph
        self.assertEqual(self._node_props("canon")["orcid"], "0000-1")
        self.assertEqual(self._node_props("canon")["email"], "canon@x.com")  # canonical's own value wins


class PromoteLinkCandidatesIntegrationTest(Neo4jIntegrationTestCase):
    def test_promoted_candidate_moves_the_relationship_and_disappears(self):
        _raw_client.upsert_nodes_batch("Publication", [("pub1", {})])
        _raw_client.upsert_nodes_batch("LinkCandidate", [("https://x.com/repo", {"url": "https://x.com/repo"})])
        _raw_client.upsert_relationships_batch(
            "Publication", "LinkCandidate", "MENTIONS_LINK", [("pub1", "https://x.com/repo", {"context": ["intro"]})]
        )
        _raw_client.upsert_nodes_batch("Repository", [("repo1", {"url": "https://github.com/x/repo"})])
        audited, sink = self._audited()

        with actor_context("integration-test"):
            audited.promote_link_candidates_batch([("https://x.com/repo", "https://github.com/x/repo")])

        entry = sink.entries[0]
        self.assertEqual(entry.entity_type, "LinkCandidate")
        self.assertEqual(entry.change_kind, "deleted")
        self.assertIsNone(self._node_props("https://x.com/repo"))  # candidate removed

        with _raw_client.driver.session() as session:
            record = session.run(
                "MATCH (:Publication {id: 'pub1'})-[r:MENTIONS_LINK]->(:Repository {id: 'repo1'}) "
                "RETURN properties(r) AS props"
            ).single()
        self.assertIsNotNone(record)  # relationship moved onto the real repository
        self.assertEqual(record["props"]["context"], ["intro"])  # properties preserved across the move


class SyncImplementsIntegrationTest(Neo4jIntegrationTestCase):
    def test_only_relationships_outside_the_confirmed_set_are_deleted(self):
        _raw_client.upsert_nodes_batch("Publication", [("pub1", {})])
        _raw_client.upsert_nodes_batch("Repository", [
            ("authors-repo", {"url": "https://github.com/org/authors-repo"}),
            ("dependency", {"url": "https://github.com/org/dependency"}),
        ])
        _raw_client.upsert_relationships_batch(
            "Repository",
            "Publication",
            "IMPLEMENTS",
            [("authors-repo", "pub1", {}), ("dependency", "pub1", {})],
        )
        audited, sink = self._audited()

        removed = audited.sync_implements_relationships_batch([
            ("pub1", ["authors-repo"]),
        ])

        self.assertEqual(removed, 1)
        with _raw_client.driver.session() as session:
            repositories = session.run(
                "MATCH (repository:Repository)-[:IMPLEMENTS]->(:Publication {id: 'pub1'}) "
                "RETURN repository.id AS id ORDER BY id"
            ).value("id")
        self.assertEqual(repositories, ["authors-repo"])
        [entry] = sink.entries
        self.assertEqual(entry.operation, "sync_implements_relationships")


class NullPropertyRemovalIntegrationTest(Neo4jIntegrationTestCase):
    def test_set_map_null_removes_stale_node_and_relationship_properties(self):
        _raw_client.upsert_nodes_batch("Publication", [(
            "pub1",
            {"code_url": '["https://github.com/org/repo"]'},
        )])
        _raw_client.upsert_nodes_batch("Repository", [(
            "repo1",
            {"url": "https://github.com/org/repo"},
        )])
        _raw_client.upsert_relationships_batch(
            "Publication",
            "Repository",
            "MENTIONS_LINK",
            [("pub1", "repo1", {"is_relevant": True})],
        )

        _raw_client.upsert_nodes_batch("Publication", [("pub1", {"code_url": None})])
        _raw_client.upsert_relationships_batch(
            "Publication",
            "Repository",
            "MENTIONS_LINK",
            [("pub1", "repo1", {"is_relevant": None})],
        )

        publication = self._node_props("pub1")
        self.assertNotIn("code_url", publication)
        with _raw_client.driver.session() as session:
            relationship = session.run(
                "MATCH (:Publication {id: 'pub1'})-[r:MENTIONS_LINK]->"
                "(:Repository {id: 'repo1'}) RETURN properties(r) AS props"
            ).single()["props"]
        self.assertNotIn("is_relevant", relationship)


class JSONLAuditSinkIntegrationTest(Neo4jIntegrationTestCase):
    def test_end_to_end_write_produces_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = JSONLAuditSink(Path(tmp) / "audit.jsonl")
            audited = AuditedNeo4jClient(_raw_client, sink)
            with actor_context("user:integration", source="pytest"):
                audited.upsert_nodes_batch("Department", [("d1", {"name": "CS"})])

            lines = (Path(tmp) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["actor"], "user:integration")
            self.assertEqual(record["source"], "pytest")
            self.assertEqual(record["entity_id"], "d1")
            self.assertEqual(record["change_kind"], "created")
            self.assertEqual(record["diff"], {"id": [None, "d1"], "name": [None, "CS"]})


class ActorContextIntegrationTest(Neo4jIntegrationTestCase):
    def test_nested_actor_context_survives_real_driver_calls(self):
        audited, sink = self._audited()
        with actor_context("etl-pipeline", source="jsonl_loader"):
            with actor_context("user:bob", source="admin-ui"):
                audited.upsert_nodes_batch("Department", [("d1", {"name": "CS"})])
            audited.upsert_nodes_batch("Department", [("d2", {"name": "Math"})])

        inner, outer = sink.entries
        self.assertEqual(inner.actor, "user:bob")
        self.assertEqual(inner.source, "admin-ui")
        self.assertEqual(outer.actor, "etl-pipeline")
        self.assertEqual(outer.source, "jsonl_loader")


if __name__ == "__main__":
    unittest.main()
