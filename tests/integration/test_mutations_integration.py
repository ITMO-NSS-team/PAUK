"""Integration tests for the delete paths against a real Neo4j instance.

Deletion is the one part of the manual-edit layer a fake client cannot
vouch for: `delete_nodes_batch` builds a `collect` + `FOREACH` query whose
validity only a real server can confirm, and the "refuse to delete a
connected node" guard is enforced by Neo4j itself, not by our Python.

Skipped unless testcontainers is installed and Docker is reachable:
    uv run --with pytest --with mongomock --with "testcontainers[neo4j]" \
      pytest tests/integration -q
"""

from __future__ import annotations

import unittest

from pauk.graph.client import Neo4jClient

try:
    from testcontainers.community.neo4j import Neo4jContainer

    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - only when the extra isn't installed
    Neo4jContainer = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR = exc

_PASSWORD = "integration-test-password"

_container = None
_client: Neo4jClient | None = None


def setUpModule():  # noqa: N802 - unittest's required hook name
    global _container, _client
    if _IMPORT_ERROR is not None:
        raise unittest.SkipTest(
            f"testcontainers not installed ({_IMPORT_ERROR}); "
            "run with --with 'testcontainers[neo4j]' to include these tests"
        )
    _container = Neo4jContainer(image="neo4j:5-community", password=_PASSWORD)
    try:
        _container.start()
    except Exception as exc:  # Docker not running, no permission, pull failure
        raise unittest.SkipTest(f"could not start a Neo4j container: {exc}") from exc
    _client = Neo4jClient(_container.get_connection_url(), _container.username, _PASSWORD)


def tearDownModule():  # noqa: N802
    if _client is not None:
        _client.close()
    if _container is not None:
        _container.stop()


class DeleteAgainstRealGraphTest(unittest.TestCase):
    def setUp(self):
        with _client.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    def test_nodes_are_deleted_and_counted(self):
        _client.upsert_nodes_batch("Person", [("A1", {"name_en": "Ivan"}), ("A2", {})])
        self.assertEqual(_client.delete_nodes_batch("Person", ["A1", "A2"]), 2)
        self.assertIsNone(_client.fetch_node_properties("Person", "A1"))

    def test_a_connected_node_survives_without_detach(self):
        # The guard that stops a careless delete from tearing edges out of
        # the graph — enforced by the query, not by Python.
        _client.upsert_nodes_batch("Person", [("A1", {})])
        _client.upsert_nodes_batch("Publication", [("W1", {})])
        _client.upsert_relationships_batch("Person", "Publication", "AUTHORED", [("A1", "W1", {})])
        self.assertEqual(_client.delete_nodes_batch("Person", ["A1"], detach=False), 0)
        self.assertIsNotNone(_client.fetch_node_properties("Person", "A1"))
        self.assertEqual(_client.delete_nodes_batch("Person", ["A1"], detach=True), 1)

    def test_deleting_an_id_that_is_not_there_removes_nothing(self):
        self.assertEqual(_client.delete_nodes_batch("Person", ["nobody"]), 0)

    def test_a_relationship_goes_and_both_nodes_stay(self):
        _client.upsert_nodes_batch("Person", [("A1", {})])
        _client.upsert_nodes_batch("Publication", [("W1", {})])
        _client.upsert_relationships_batch("Person", "Publication", "AUTHORED", [("A1", "W1", {})])
        removed = _client.delete_relationships_batch(
            "Person", "Publication", "AUTHORED", [("A1", "W1")])
        self.assertEqual(removed, 1)
        self.assertIsNotNone(_client.fetch_node_properties("Person", "A1"))
        self.assertIsNotNone(_client.fetch_node_properties("Publication", "W1"))

    def test_reading_a_node_that_is_not_there(self):
        self.assertIsNone(_client.fetch_node_properties("Person", "nobody"))


if __name__ == "__main__":
    unittest.main()
