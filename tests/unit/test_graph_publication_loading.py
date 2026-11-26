"""Regression scenarios for graph publication loading and partial source authors."""

import copy
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from pauk.graph.client import Neo4jClient
from pauk.graph.dedup import dedup_graph_persons
from pauk.graph.person_resolution import Decision
from pauk.graph.person_resolution_single import SingleVerdict


class BulkGraph:
    def __init__(self, people, publications):
        self.people = people
        self.publications = publications
        self.publication_reads = 0
        self.authorship_reads = 0
        self.writes = []

    def fetch_persons_for_dedup(self):
        return self.people

    def fetch_person_publications_for_dedup(self):
        self.publication_reads += 1
        return self.publications

    def fetch_person_authorships_for_dedup(self):
        self.authorship_reads += 1
        return [{"person_id": row["id"], "authorships": [
            {"publication_id": work, "position": 2, "affiliation": "Lab"}
            for work in row.get("publication_ids", [])
        ]} for row in self.people]

    def fetch_publication_fields(self):
        raise AssertionError("Publication fields must come from the same bulk read")

    def upsert_person_nodes_batch(self, rows):
        self.writes.extend(rows)

    def merge_person_nodes_batch(self, rows):
        self.writes.extend(rows)
        return len(rows)


class RecordingJudge:
    def __init__(self):
        self.calls = []

    def single_many(self, items):
        self.calls.extend(items)
        return {context.pair_id: SingleVerdict(Decision.MERGE, 0.99, "same name")
                for context, _ in items}


class GraphPublicationLoadingTest(unittest.TestCase):
    def graph(self, authors):
        publications = [
            {"id": "W1", "doi": "10.1234/new", "title": "Current", "year": 2030,
             "fields": ["Physics"], "merged_ids": ["W0"], "versions": json.dumps([
                 {"openalex_id": "W0", "doi": "10.1234/old", "year": 2018, "authors": authors},
             ])},
            {"id": "W2", "doi": "10.1234/old", "title": "Earlier copy", "year": 2024,
             "fields": ["Physics"], "merged_ids": [], "versions": "[]"},
        ]
        people = [
            {"id": person_id, "name_raw": name, "email": email, "is_itmo": True,
             "publication_ids": works, "department_ids": ["D"]}
            for person_id, name, email, works in (
                ("A", "Ivan Petrov", "a@example.org", ["W1"]),
                ("B", "Ivan Petrov", "b@example.org", ["W2"]),
                ("C", "Jane Smith", None, ["W1", "W2"]),
            )
        ]
        return BulkGraph(people, publications)

    def run_judge(self, client):
        before = copy.deepcopy((client.people, client.publications))
        judge = RecordingJudge()
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
            removed, report = dedup_graph_persons(client, {}, models=judge)
        self.assertEqual(removed, 0)
        self.assertEqual(client.writes, [])
        self.assertEqual((client.people, client.publications), before)
        self.assertEqual(client.publication_reads, 1)
        self.assertEqual(client.authorship_reads, 1)
        self.assertTrue(any(row["status"] == "held" for row in report))
        return next((context, evidence) for context, evidence in judge.calls
                    if {evidence.person_a, evidence.person_b} == {"A", "B"})

    def test_each_valid_source_author_survives_malformed_neighbors(self):
        for invalid in ({}, {"name": "Unknown"}, None, "bad", [],
                        {"person_id": None}, {"person_id": 42}, {"person_id": True},
                        {"person_id": ""}, {"person_id": "  "}, {"person_id": []}):
            for reverse in (False, True):
                with self.subTest(invalid=invalid, reverse=reverse):
                    authors = [{"person_id": "A", "position": "unknown"}, invalid]
                    if reverse:
                        authors.reverse()
                    context, evidence = self.run_judge(self.graph(authors))
                    self.assertEqual((evidence.shared_publications, evidence.shared_coauthors), (1, 0))
                    years = {"A": [2018, 2018], "B": [2024, 2024]}
                    for side, person in (("a", evidence.person_a), ("b", evidence.person_b)):
                        self.assertEqual(context.source_context["persons"][side]["observed_work_year_range"],
                                         years[person])

    def test_duplicate_valid_authors_do_not_change_context(self):
        baseline, _ = self.run_judge(self.graph([{"person_id": "A"}]))
        duplicated, _ = self.run_judge(self.graph([{"person_id": "A"}, {}, {"person_id": "A"}]))
        self.assertEqual(duplicated.source_context, baseline.source_context)

    def test_no_valid_author_does_not_invent_source_ownership(self):
        for authors in ([], [{}, None], None, "unknown", {"person_id": "A"}):
            with self.subTest(authors=authors):
                context, evidence = self.run_judge(self.graph(authors))
                side = "a" if evidence.person_a == "A" else "b"
                self.assertEqual(context.source_context["persons"][side]["observed_work_year_range"], [])
                self.assertEqual(evidence.shared_publications, 1)

    def test_bulk_metadata_reaches_both_planners_without_authorship_metadata_read_in_rule_mode(self):
        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                client = self.graph([{"person_id": "A"}])
                target = "plan_person_merges_resolved" if resolved else "plan_person_merges"
                with patch("pauk.graph.dedup." + target, return_value=([], [])) as planner:
                    dedup_graph_persons(client, {}, models=RecordingJudge() if resolved else None)
                details = planner.call_args.kwargs["publication_details"]
                self.assertEqual(details["W0"]["doi"], "10.1234/old")
                self.assertEqual(details["W0"]["source_person_ids"], ["A"])
                self.assertEqual(details["W2"]["year"], 2024)
                self.assertEqual(details["W2"]["title"], "Earlier copy")
                self.assertEqual(planner.call_args.kwargs["fields_of"],
                                 {"W1": {"Physics"}, "W2": {"Physics"}})
                self.assertEqual(client.publication_reads, 1)
                self.assertEqual(client.authorship_reads, int(resolved))

    def test_coauthor_count_does_not_multiply_publication_reads_or_version_payload(self):
        marker = "unique-ledger-content-" + "x" * 22000
        publication = {"id": "W1", "doi": "10.1234/a", "merged_ids": [], "fields": [],
                       "versions": json.dumps([{"openalex_id": "W1", "title": marker}])}
        expected_payload_bytes = len(publication["versions"].encode())
        for count in (1, 132, 133):
            for resolved in (False, True):
                with self.subTest(count=count, resolved=resolved):
                    client = BulkGraph([
                        {"id": f"A{i}", "name_raw": f"Person{i} Author{i}", "is_itmo": True,
                         "publication_ids": ["W1"]} for i in range(count)
                    ], [copy.deepcopy(publication)])
                    target = "plan_person_merges_resolved" if resolved else "plan_person_merges"
                    with patch("pauk.graph.dedup." + target, return_value=([], [])) as planner:
                        self.assertEqual(dedup_graph_persons(client, {}, models=RecordingJudge() if resolved else None),
                                         (0, []))
                    self.assertEqual(client.publication_reads, 1)
                    self.assertEqual(len(planner.call_args.args[0]), count)
                    self.assertEqual(sum(len(row["versions"].encode()) for row in client.publications),
                                     expected_payload_bytes)
                    self.assertTrue(all("publication_details" not in person for person in client.people))

    def test_empty_graph_does_not_read_publications_or_request_models(self):
        client = BulkGraph([], [{"id": "unrelated", "versions": "[]"}])
        judge = RecordingJudge()
        self.assertEqual(dedup_graph_persons(client, {}, models=judge), (0, []))
        self.assertEqual(client.publication_reads, 0)
        self.assertEqual(client.authorship_reads, 0)
        self.assertEqual(judge.calls, [])
        self.assertEqual(client.writes, [])

    def test_publication_read_failure_is_not_silently_treated_as_missing_evidence(self):
        client = self.graph([{"person_id": "A"}])
        with (
            patch.object(client, "fetch_person_publications_for_dedup", side_effect=RuntimeError("database unavailable")),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            dedup_graph_persons(client, {})
        self.assertEqual(client.writes, [])

    def test_query_returns_publications_separately_without_author_join(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        transaction = Mock()
        transaction.run.return_value = []
        session = client.driver.session.return_value.__enter__.return_value
        session.execute_read.side_effect = lambda read: read(transaction)
        self.assertEqual(client.fetch_persons_for_dedup(), [])
        people_query = transaction.run.call_args.args[0]
        self.assertNotIn("w.versions", people_query)
        self.assertNotIn("publication_details", people_query)
        self.assertEqual(client.fetch_person_authorships_for_dedup(), [])
        authorship_query = transaction.run.call_args.args[0]
        for field in ("w.versions", "w.title", "w.year", "w.doi"):
            self.assertNotIn(field, authorship_query)
        self.assertEqual(client.fetch_person_publications_for_dedup(), [])
        publications_query = transaction.run.call_args.args[0]
        self.assertIn("w.versions", publications_query)
        normalized = " ".join(publications_query.split())
        self.assertIn("WHERE EXISTS { MATCH (:Person)-[:AUTHORED]->(w) }", normalized)
        outer_query = normalized.split("WHERE EXISTS", 1)[0]
        self.assertNotIn("AUTHORED", outer_query)
        self.assertNotIn(":Person", outer_query)


if __name__ == "__main__":
    unittest.main()
