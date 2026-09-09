"""Unit tests for export.py: cypher_dict, retry logic, load_db's table contract.

Stub driver only, no real Neo4j - these tests catch shape/logic regressions
in this module's own code, not whether a query is structurally valid against
the real graph model (that needs a real database, out of scope here).
"""

from __future__ import annotations

import unittest
from unittest import mock

from neo4j.exceptions import ServiceUnavailable

from pauk.cache.export import GraphSnapshotExporter, cypher_dict, load_db
from pauk.settings import Settings


class FakeRecord:
    """Minimal stand-in for neo4j.Record - only what cypher_dict() calls."""

    def __init__(self, mapping: dict):
        self._mapping = mapping

    def data(self):
        return dict(self._mapping)


class SequentialFakeDriver:
    """Driver stub with no network: execute_query() returns queued responses in order.

    load_db() always issues its queries in the same fixed order, so matching
    responses by position is simpler and more robust than matching by query text.
    """

    def __init__(self, responses: list[list[dict]]):
        self._responses = iter(responses)
        self.queries: list[str] = []

    def execute_query(self, query, **params):
        self.queries.append(query)
        try:
            rows = next(self._responses)
        except StopIteration as exc:
            raise AssertionError(f"more queries than prepared responses: {query!r}") from exc
        return [FakeRecord(r) for r in rows], None, None


class CypherDictTest(unittest.TestCase):
    def test_returns_rows_as_column_keyed_dicts(self):
        driver = SequentialFakeDriver([[{"id": "A1", "name_ru": "Ivanov"}]])
        rows = cypher_dict(driver, "MATCH (p:Person) RETURN p.id AS id, p.name_ru AS name_ru")
        self.assertEqual(rows, [{"id": "A1", "name_ru": "Ivanov"}])


class ExecuteRetryingTest(unittest.TestCase):
    def test_recovers_after_one_transient_failure(self):
        attempts = {"n": 0}

        class FlakyDriver:
            def execute_query(self, query, **params):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise ServiceUnavailable("temporarily unavailable")
                return [FakeRecord({"id": "ok"})], None, None

        with mock.patch("pauk.cache.export.time.sleep") as sleep_mock:
            rows = cypher_dict(FlakyDriver(), "MATCH (n) RETURN n.id AS id")

        self.assertEqual(rows, [{"id": "ok"}])
        self.assertEqual(attempts["n"], 2)
        sleep_mock.assert_called_once()

    def test_gives_up_after_exhausting_all_retries(self):
        class AlwaysFailingDriver:
            def execute_query(self, query, **params):
                raise ServiceUnavailable("unavailable")

        with mock.patch("pauk.cache.export.time.sleep"), self.assertRaises(ServiceUnavailable):
            cypher_dict(AlwaysFailingDriver(), "MATCH (n) RETURN n.id AS id")


# Fixed order load_db() issues its queries in - callers (GraphSnapshotExporter,
# read_snapshot consumers) depend on exactly these keys existing in the result.
TABLE_ORDER = [
    "persons",
    "publications",
    "repositories",
    "departments",
    "organizations",
    "authorship",
    "person_depts",
    "pub_depts",
    "repo_pubs",
    "mentions_repos",
    "mentions_candidates",
    "repo_persons",
    "repo_depts",
]


class LoadDbTest(unittest.TestCase):
    def test_returns_exactly_the_expected_tables(self):
        db = load_db(SequentialFakeDriver([[] for _ in TABLE_ORDER]))
        self.assertEqual(set(db.keys()), set(TABLE_ORDER))

    def test_maps_each_query_response_to_its_own_table(self):
        # Responses are tagged with their table name, not real field data - a
        # stub driver can't verify actual Cypher field names, only that
        # response N lands under the right dict key (a real, easy mistake
        # with thirteen near-identical cypher_dict() calls in a row).
        responses = [[{"table": name}] for name in TABLE_ORDER]
        db = load_db(SequentialFakeDriver(responses))
        for name in TABLE_ORDER:
            self.assertEqual(db[name], [{"table": name}])

    def test_person_queries_filter_by_property_not_legacy_label(self):
        # Regression guard: load_db() used to MATCH (p:Person:Itmo), a label
        # the ingestion pipeline stopped writing after the is_itmo migration -
        # any author added afterward was silently invisible to this query.
        driver = SequentialFakeDriver([[] for _ in TABLE_ORDER])
        load_db(driver)
        combined = " ".join(driver.queries)
        self.assertNotIn(":Itmo", combined)
        self.assertIn("{is_itmo: true}", combined)


class GraphSnapshotExporterTest(unittest.TestCase):
    def test_export_rejects_empty_neo4j_password(self):
        # Checked before opening the driver, so a missing password fails
        # fast with a clear message instead of a late auth error.
        config = Settings(neo4j_password="")
        with self.assertRaises(ValueError):
            GraphSnapshotExporter(config).export()


if __name__ == "__main__":
    unittest.main()
