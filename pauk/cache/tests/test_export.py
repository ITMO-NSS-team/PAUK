"""Unit tests for export.py: cypher_dict, retry logic, load_db's table contract.

Stub driver only, no real Neo4j - these tests catch shape/logic regressions
in this module's own code, not whether a query is structurally valid against
the real graph model (that needs a real database, out of scope here).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neo4j.exceptions import ServiceUnavailable

from pauk.cache.export import SNAPSHOT_GROUPS, GraphSnapshotExporter, cypher_dict, load_db
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

    def test_persons_and_authorship_include_external_authors(self):
        # External coauthors are on the map too (hidden by default), so both
        # tables take every Person and carry is_itmo instead of filtering by it.
        driver = SequentialFakeDriver([[] for _ in TABLE_ORDER])
        load_db(driver)
        persons_query = driver.queries[TABLE_ORDER.index("persons")]
        authorship_query = driver.queries[TABLE_ORDER.index("authorship")]
        self.assertIn("MATCH (p:Person) ", persons_query)
        self.assertIn("AS is_itmo", persons_query)
        self.assertIn("MATCH (p:Person)-[rel:AUTHORED]->", authorship_query)


class LoadDbSubsetTest(unittest.TestCase):
    def test_runs_only_the_requested_queries(self):
        driver = SequentialFakeDriver([[{"table": "repositories"}], [{"table": "repo_pubs"}]])
        db = load_db(driver, {"repo_pubs", "repositories"})
        self.assertEqual(db, {"repositories": [{"table": "repositories"}], "repo_pubs": [{"table": "repo_pubs"}]})
        self.assertEqual(len(driver.queries), 2)

    def test_unknown_table_is_an_error_not_an_empty_result(self):
        with self.assertRaises(ValueError):
            load_db(SequentialFakeDriver([]), {"repos"})


class SnapshotGroupsTest(unittest.TestCase):
    def test_groups_cover_every_table_and_nothing_else(self):
        covered = {table for tables in SNAPSHOT_GROUPS.values() for table in tables}
        self.assertEqual(covered, set(TABLE_ORDER))

    def test_each_entity_group_takes_every_relationship_table_that_references_it(self):
        # Deleting a repository removes its IMPLEMENTS/CONTRIBUTED_TO/
        # DEVELOPED_BY/MENTIONS_LINK edges too - all of those must be re-read.
        self.assertTrue(
            {"repositories", "repo_pubs", "repo_persons", "repo_depts", "mentions_repos"} <= set(SNAPSHOT_GROUPS["repos"])
        )
        self.assertTrue({"persons", "authorship", "person_depts", "repo_persons"} <= set(SNAPSHOT_GROUPS["persons"]))


class FakeDriverWithLifecycle(SequentialFakeDriver):
    def verify_connectivity(self):
        pass

    def close(self):
        pass


class PartialExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Settings(neo4j_password="x", data_dir=Path(self.tmp.name))
        self.cache_dir = self.config.cache_dir
        self.cache_dir.mkdir()
        self.base = {name: [{"from": "base", "table": name}] for name in TABLE_ORDER}
        self.base["repositories"] = [{"id": "R1"}, {"id": "R2"}]
        self.base_path = self.cache_dir / "graph_snapshot_01-09-2026.json"
        self.base_path.write_text(json.dumps(self.base))

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, responses, only, output):
        driver = FakeDriverWithLifecycle(responses)
        with mock.patch("pauk.cache.export.GraphDatabase.driver", return_value=driver):
            GraphSnapshotExporter(self.config).export(output, only=only)
        return driver

    def test_repos_replaces_repo_tables_and_keeps_the_rest_from_the_newest_snapshot(self):
        # One repository was deleted along with its edges.
        fresh = {"repositories": [{"id": "R1"}], "repo_pubs": [], "mentions_repos": [], "repo_persons": [], "repo_depts": []}
        order = [name for name in TABLE_ORDER if name in fresh]
        output = self.cache_dir / "graph_snapshot_02-09-2026.json"

        driver = self.export([fresh[name] for name in order], ["repos"], output)

        result = json.loads(output.read_text())
        self.assertEqual(set(result), set(TABLE_ORDER))
        for name in TABLE_ORDER:
            self.assertEqual(result[name], fresh.get(name, self.base[name]), name)
        self.assertEqual(len(driver.queries), len(fresh))
        self.assertEqual(json.loads(self.base_path.read_text()), self.base)  # base snapshot untouched

    def test_several_groups_share_tables_without_querying_them_twice(self):
        tables = set(SNAPSHOT_GROUPS["repos"]) | set(SNAPSHOT_GROUPS["persons"])
        output = self.cache_dir / "graph_snapshot_02-09-2026.json"
        driver = self.export([[] for name in TABLE_ORDER if name in tables], ["repos", "persons"], output)
        self.assertEqual(len(driver.queries), len(tables))

    def test_no_snapshot_to_base_on_is_an_error(self):
        self.base_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.export([], ["repos"], self.cache_dir / "out.json")

    def test_base_missing_a_table_that_is_not_re_read_is_an_error(self):
        del self.base["authorship"]
        self.base_path.write_text(json.dumps(self.base))
        with self.assertRaises(ValueError):
            self.export([], ["repos"], self.cache_dir / "out.json")

    def test_unknown_group_is_an_error(self):
        with self.assertRaises(ValueError):
            self.export([], ["repositories"], self.cache_dir / "out.json")


class GraphSnapshotExporterTest(unittest.TestCase):
    def test_export_rejects_empty_neo4j_password(self):
        # Checked before opening the driver, so a missing password fails
        # fast with a clear message instead of a late auth error.
        config = Settings(neo4j_password="")
        with self.assertRaises(ValueError):
            GraphSnapshotExporter(config).export()


if __name__ == "__main__":
    unittest.main()
