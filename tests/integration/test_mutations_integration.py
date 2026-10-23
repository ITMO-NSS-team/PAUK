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

import tempfile
import unittest
from itertools import permutations, product
from pathlib import Path
from types import SimpleNamespace

import mongomock

from pauk.graph.audit import AuditedNeo4jClient
from pauk.graph.client import Neo4jClient
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.mutations import merge_nodes
from pauk.graph.unmerge import split_person
from pauk.models import Person
from pauk.pipeline.stages.dedup import DedupStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore

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
        # the graph, enforced by the query and not by Python.
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


class MergePreservationAgainstRealGraphTest(unittest.TestCase):
    def setUp(self):
        with _client.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    def corresponding(self):
        with _client.driver.session() as session:
            record = session.run(
                "MATCH (:Person {id: 'A1'})-[r:AUTHORED]->(:Publication {id: 'W1'}) "
                "RETURN r.is_corresponding AS value"
            ).single()
        return record["value"]

    def test_corresponding_survives_publish_and_both_fold_directions(self):
        for mode, first, second in product(("publish", "person", "publication"), (None, False, True), (None, False, True)):
            with self.subTest(mode=mode, first=first, second=second):
                self.setUp()
                _client.upsert_person_nodes_batch([("A1", {}), ("A2", {})])
                _client.upsert_nodes_batch("Publication", [("W1", {}), ("W2", {})])
                _client.upsert_relationships_batch("Person", "Publication", "AUTHORED", [
                    ("A1", "W1", {} if first is None else {"is_corresponding": first}),
                    ("A2" if mode == "person" else "A1", "W2" if mode == "publication" else "W1",
                     {} if second is None else {"is_corresponding": second}),
                ])
                if mode == "person":
                    self.assertEqual(_client.merge_person_nodes_batch([("A2", "A1")]), 1)
                elif mode == "publication":
                    self.assertEqual(_client.merge_publication_nodes_batch([("W2", "W1")]), 1)
                expected = None if first is None and second is None else bool(first or second)
                self.assertEqual(self.corresponding(), expected)

    def test_multiple_duplicates_and_republish_preserve_positive_evidence(self):
        for label in ("Person", "Publication"):
            for flags in ((True, False), (False, True)):
                with self.subTest(label=label, flags=flags):
                    self.setUp()
                    _client.upsert_person_nodes_batch([(f"A{i}", {}) for i in (1, 2, 3)])
                    _client.upsert_nodes_batch("Publication", [(f"W{i}", {}) for i in (1, 2, 3)])
                    rows = [("A1", "W1", {"is_corresponding": False})]
                    rows += [
                        (f"A{i}" if label == "Person" else "A1", f"W{i}" if label == "Publication" else "W1",
                         {"is_corresponding": flag})
                        for i, flag in zip((2, 3), flags, strict=True)
                    ]
                    _client.upsert_relationships_batch("Person", "Publication", "AUTHORED", rows)
                    if label == "Person":
                        _client.merge_person_nodes_batch([("A2", "A1"), ("A3", "A1")])
                    else:
                        _client.merge_publication_nodes_batch([("W2", "W1"), ("W3", "W1")])
                    self.assertTrue(self.corresponding())
                    self.assertEqual(_client.upsert_relationships_batch(
                        "Person", "Publication", "AUTHORED",
                        [("A1", "W1", {"is_corresponding": False})] * 3,
                    ), 3)
                    self.assertTrue(self.corresponding())

    def test_graph_person_dedup_preserves_full_properties(self):
        import json

        from pauk.graph.dedup import dedup_graph_persons

        for index in (1, 2, 3):
            _client.upsert_person_nodes_batch([(f"A{index}", {
                "name_raw": "Ivan Petrov", "is_member": True, "orcid": "0000-0001",
                "emails": [f"a{index}@example.org"], "other_names": [f"Alias {index}"],
                "affiliations": json.dumps([{"name": f"Institute {index}", "source": "openalex"}]),
                "biography": f"Profile {index}",
            })])
        self.assertEqual(dedup_graph_persons(_client, {})[0], 2)
        survivor = _client.fetch_node_properties("Person", "A1")
        self.assertEqual(survivor["emails"], ["a1@example.org", "a2@example.org", "a3@example.org"])
        self.assertEqual(survivor["other_names"], ["Alias 1", "Alias 2", "Alias 3"])
        self.assertEqual(len(json.loads(survivor["affiliations"])), 3)
        self.assertEqual(survivor["biography"], "Profile 1")

    def test_partial_republish_keeps_graph_merge_evidence(self):
        from pauk.graph.dedup import dedup_graph_persons

        rows = [Person(
            id=f"A{i}", is_member=True, name_raw="Ivan Petrov", orcid="same-orcid",
            emails=[f"p{i}@example.org"], other_names=[f"Alias {i}"],
            affiliations=[{"name": f"University {i}", "source": "openalex"}],
        ).model_dump(mode="json") for i in (1, 2)]
        load_prepared_rows(_client, {"persons.jsonl": rows})
        self.assertEqual(dedup_graph_persons(_client, {})[0], 1)
        before = _client.fetch_node_properties("Person", "A1")
        for _ in range(2):
            load_prepared_rows(_client, {"persons.jsonl": [rows[0]]})
            after = _client.fetch_node_properties("Person", "A1")
            for field in ("emails", "other_names", "affiliations", "name_variants", "merged_ids"):
                self.assertEqual(after[field], before[field])

    def test_fold_chains_are_order_independent_for_people_and_publications(self):
        for label, order in product(("Person", "Publication"), permutations([("X1", "X2"), ("X2", "X3")])):
            with self.subTest(label=label, order=order):
                self.setUp()
                _client.upsert_nodes_batch(label, [(f"X{i}", {"merged_ids": [f"old{i}"], "detail": f"value{i}"}) for i in (1, 2)])
                _client.upsert_nodes_batch(label, [("X3", {})])
                _client.upsert_nodes_batch("Person" if label == "Publication" else "Publication", [("other", {})])
                rows = [
                    (f"X{i}" if label == "Person" else "other", "other" if label == "Person" else f"X{i}",
                     {"is_corresponding": i == 1}) for i in (1, 2)
                ]
                _client.upsert_relationships_batch("Person", "Publication", "AUTHORED", rows)
                fold = _client.merge_person_nodes_batch if label == "Person" else _client.merge_publication_nodes_batch
                self.assertEqual(fold(list(order)), 2)
                survivor = _client.fetch_node_properties(label, "X3")
                self.assertEqual(set(survivor["merged_ids"]), {"old1", "old2"})
                self.assertEqual(survivor["detail"], "value2")
                with _client.driver.session() as session:
                    flags = session.run("MATCH ()-[r:AUTHORED]->() RETURN r.is_corresponding AS flag").value()
                self.assertEqual(flags, [True])

    def test_loader_chain_preserves_relationships_on_removed_ids(self):
        load_prepared_rows(_client, {
            "publications.jsonl": [{"id": "W1", "title": "Work"}],
            "persons.jsonl": [
                {"id": "A", "other_names": ["A"], "authored": [{"publication_id": "W1", "is_corresponding": True}]},
                {"id": "B", "other_names": ["B"], "merged_ids": ["A"]},
                {"id": "C", "other_names": ["C"], "merged_ids": ["B"]},
            ],
        })
        self.assertEqual(set(_client.fetch_node_properties("Person", "C")["other_names"]), {"A", "B", "C"})
        with _client.driver.session() as session:
            rows = session.run("MATCH (p:Person)-[r:AUTHORED]->() RETURN p.id AS id, r.is_corresponding AS flag").data()
        self.assertEqual(rows, [{"id": "C", "flag": True}])

    def test_authored_scalar_priority_follows_chain_not_input_order(self):
        for label, order, existing in product(
            ("Person", "Publication"), permutations([("A", "B"), ("B", "C")]), (False, True)
        ):
            with self.subTest(label=label, order=order, existing=existing):
                self.setUp()
                _client.upsert_nodes_batch(label, [(identifier, {}) for identifier in ("A", "B", "C")])
                other = "Publication" if label == "Person" else "Person"
                _client.upsert_nodes_batch(other, [("X", {})])
                edges = []
                for identifier, position in [("A", 7), ("B", 3)] + ([("C", 1)] if existing else []):
                    src, tgt = (identifier, "X") if label == "Person" else ("X", identifier)
                    edges.append((src, tgt, {"position": position, "affiliation": identifier,
                                             "is_corresponding": identifier == "A"}))
                _client.upsert_relationships_batch("Person", "Publication", "AUTHORED", edges)
                fold = _client.merge_person_nodes_batch if label == "Person" else _client.merge_publication_nodes_batch
                fold(list(order))
                with _client.driver.session() as session:
                    props = session.run("MATCH ()-[r:AUTHORED]->() RETURN properties(r)").value()
                self.assertEqual(len(props), 1)
                self.assertEqual(props[0]["position"], 1 if existing else 3)
                self.assertEqual(props[0]["affiliation"], "C" if existing else "B")
                self.assertTrue(props[0]["is_corresponding"])

    def test_loader_resolves_a_chain_spanning_multiple_write_chunks(self):
        count = 2002
        rows = [{"id": f"A{i}", "merged_ids": [f"A{i - 1}"] if i else []} for i in range(count + 1)]
        rows[0]["authored"] = [{"publication_id": "W1", "is_corresponding": True}]
        load_prepared_rows(_client, {
            "publications.jsonl": [{"id": "W1", "title": "Work"}], "persons.jsonl": rows,
        })
        self.assertEqual(_client.count_nodes("Person"), 1)
        self.assertEqual(len(_client.fetch_node_properties("Person", f"A{count}")["merged_ids"]), count)
        with _client.driver.session() as session:
            edges = session.run("MATCH (p:Person)-[r:AUTHORED]->() RETURN p.id AS id, r.is_corresponding AS flag").data()
        self.assertEqual(edges, [{"id": f"A{count}", "flag": True}])

    def _undo_fixture(self, flags):
        db = mongomock.MongoClient().dedup_test
        people = [Person(
            id=f"A{i}", is_member=True, other_names=[f"alias{i}"],
            authored=[{"publication_id": "W1", "is_corresponding": flag}],
        ) for i, flag in enumerate(flags, 1)]
        PreparedStore(db, "test").write_models("persons", people)
        _client.upsert_nodes_batch("Publication", [("W1", {})])
        load_prepared_rows(_client, {"persons.jsonl": [p.model_dump(mode="json") for p in people]})
        for i in range(2, len(people) + 1):
            merge_nodes(_client, "Person", f"A{i}", "A1")
        return db

    def test_undo_recomputes_corresponding_and_records_the_change(self):
        for first, second in product((False, True), repeat=2):
            with self.subTest(first=first, second=second):
                self.setUp()
                db = self._undo_fixture([first, second])
                entries = []
                audited = AuditedNeo4jClient(_client, SimpleNamespace(write=entries.extend))
                split_person(audited, db, ["A1", "A2"])
                self.assertEqual(self.corresponding(), first)
                with _client.driver.session() as session:
                    flag = session.run("MATCH (:Person {id:'A2'})-[r:AUTHORED]->() RETURN r.is_corresponding").single()[0]
                self.assertEqual(flag, second)
                if not first and second:
                    self.assertTrue(any(entry.operation == "set_relationship_properties" and
                                        entry.diff.get("is_corresponding") == (True, False) for entry in entries))

    def test_undo_keeps_evidence_of_a_third_member_and_handles_missing_source(self):
        for third, missing in product((False, True), (False, True)):
            with self.subTest(third=third, missing=missing):
                self.setUp()
                db = self._undo_fixture([False, True, third])
                if missing:
                    db.persons.delete_one({"id": "A3"})
                split_person(_client, db, ["A1", "A2"])
                self.assertEqual(self.corresponding(), True if missing else third)
                survivor = _client.fetch_node_properties("Person", "A1")
                self.assertIn("alias3", survivor["other_names"])
                self.assertEqual(survivor["merged_ids"], ["A3"])
                if not missing:
                    self.assertNotIn("alias2", survivor["other_names"])

    def test_undo_follows_publication_aliases(self):
        db = self._undo_fixture([False, True])
        _client.upsert_nodes_batch("Publication", [("W2", {})])
        merge_nodes(_client, "Publication", "W1", "W2")
        split_person(_client, db, ["A1", "A2"])
        with _client.driver.session() as session:
            rows = session.run("MATCH (p:Person)-[r:AUTHORED]->(w:Publication) "
                               "RETURN p.id AS id, w.id AS work, r.is_corresponding AS flag ORDER BY id").data()
        self.assertEqual(rows, [{"id": "A1", "work": "W2", "flag": False},
                                {"id": "A2", "work": "W2", "flag": True}])

    def test_prepared_dedup_archive_restores_deleted_person_and_survives_republish(self):
        db = mongomock.MongoClient().dedup_test
        prepared = PreparedStore(db, "test")
        people = [
            Person(id=f"A{i}", name_raw="Ivan Smirnov", is_member=True, orcid="same",
                   emails=[f"person{i}@example.org"],
                   authored=[{"publication_id": "W1", "is_corresponding": i == 2}])
            for i in (1, 2)
        ]
        prepared.write_models("persons", people)
        _client.upsert_nodes_batch("Publication", [("W1", {})])
        with tempfile.TemporaryDirectory() as directory:
            stage = DedupStage(prepared, RawStore(db, "test"), Settings(
                data_dir=Path(directory), person_resolution_enabled=False,
            ))
            stage._dedup_persons()
        load_prepared_rows(_client, {"persons.jsonl": list(prepared.read_rows("persons"))})
        self.assertEqual(_client.count_nodes("Person"), 1)
        self.assertIsNone(db.persons.find_one({"id": "A2"}))
        split_person(_client, db, ["A1", "A2"])
        load_prepared_rows(_client, {"persons.jsonl": list(prepared.read_rows("persons"))})
        self.assertEqual(_client.count_nodes("Person"), 2)
        self.assertFalse(self.corresponding())
        for i in (1, 2):
            node = _client.fetch_node_properties("Person", f"A{i}")
            self.assertEqual(node["emails"], [f"person{i}@example.org"])
            self.assertEqual(node["merged_ids"], [])
        with _client.driver.session() as session:
            self.assertTrue(session.run(
                "MATCH (:Person {id:'A2'})-[r:AUTHORED]->(:Publication {id:'W1'}) RETURN r.is_corresponding"
            ).single()[0])

    def test_cycle_is_rejected_without_deleting_nodes(self):
        _client.upsert_person_nodes_batch([("A1", {}), ("A2", {})])
        with self.assertRaises(ValueError):
            _client.merge_person_nodes_batch([("A1", "A2"), ("A2", "A1")])
        self.assertEqual(_client.count_nodes("Person"), 2)

    def test_explicit_relationship_restore_preserves_other_fields_and_does_not_create_edges(self):
        _client.upsert_person_nodes_batch([("A1", {}), ("A2", {})])
        _client.upsert_nodes_batch("Publication", [("W1", {})])
        _client.upsert_relationships_batch("Person", "Publication", "AUTHORED", [
            ("A1", "W1", {"is_corresponding": True, "affiliation": "Keep this"})])
        self.assertEqual(_client.set_relationship_properties_batch("Person", "Publication", "AUTHORED", [
            ("A1", "W1", {"is_corresponding": None}), ("A2", "W1", {"is_corresponding": False})]), 1)
        with _client.driver.session() as session:
            rows = session.run("MATCH ()-[r:AUTHORED]->() RETURN properties(r) AS props").value()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("is_corresponding", rows[0])
        self.assertEqual(rows[0]["affiliation"], "Keep this")


if __name__ == "__main__":
    unittest.main()
