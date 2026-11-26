import copy
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from pauk.graph.client import Neo4jClient
from pauk.graph.dedup import dedup_graph_persons
from pauk.graph.person_resolution import Decision
from pauk.graph.person_resolution_single import SingleVerdict, full_payload
from pauk.models import Person
from pauk.pipeline.stages.dedup import plan_person_merges


class GraphDedupIngressTest(unittest.TestCase):
    def test_query_reads_all_emails_and_confirmed_publication_identity(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        transaction = Mock()
        transaction.run.return_value = []
        session = client.driver.session.return_value.__enter__.return_value
        session.execute_read.side_effect = lambda read: read(transaction)
        self.assertEqual(client.fetch_persons_for_dedup(), [])
        query = transaction.run.call_args.args[0]
        self.assertIn("p.emails AS emails", query)
        self.assertNotIn("publication_details", query)
        self.assertNotIn("w.versions", query)
        self.assertEqual(client.fetch_person_publications_for_dedup(), [])
        publication_query = transaction.run.call_args.args[0]
        self.assertIn("w.doi AS doi", publication_query)
        self.assertIn("w.merged_ids AS merged_ids", publication_query)
        self.assertIn("w.versions AS versions", publication_query)

    def test_secondary_email_overlap_has_the_same_result_as_prepared(self):
        rows = [
            {"id": person_id, "name_raw": "Ivan Petrov", "is_itmo": True,
             "department_ids": ["D1"], "email": email, "emails": ["shared@example.org"]}
            for person_id, email in (("A", "a@example.org"), ("B", "b@example.org"))
        ]
        groups, _ = plan_person_merges([Person(**row) for row in rows], {})
        expected = sum(len(duplicates) for _, duplicates in groups)
        self.assertEqual(expected, 1)
        class Judge:
            def single_many(self, items):
                return {context.pair_id: SingleVerdict(Decision.MERGE, 0.99, "same person")
                        for context, _ in items}

        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                client = Mock()
                client.fetch_persons_for_dedup.return_value = copy.deepcopy(rows)
                client.fetch_person_publications_for_dedup.return_value = []
                client.merge_person_nodes_batch.side_effect = len
                with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.995):
                    removed, _ = dedup_graph_persons(client, {}, models=Judge() if resolved else None)
                self.assertEqual(removed, expected)

    def test_malformed_optional_context_preserves_usable_evidence(self):
        for position in ("unknown", True, [], -1, 0):
            for years in (2025, "2025", {"year": 2025}, [2024, "bad", True, 2025]):
                with self.subTest(position=position, years=years):
                    source_rows = [
                        {"person_id": person_id, "affiliations": json.dumps([
                            {"name": "ITMO", "source": "orcid", "years": years},
                            {"name": "Lab", "source": "openalex", "years": [2023]},
                        ]), "authorships": [{
                            "publication_id": "W1", "position": position,
                            "affiliation": "Physics", "affiliation_source": "openalex",
                            "is_corresponding": "false",
                        }]}
                        for person_id in ("A1", "A2")
                    ]
                    before = copy.deepcopy(source_rows)

                    class Client:
                        def __init__(self, rows):
                            self.rows = rows

                        def fetch_persons_for_dedup(self):
                            return [{"id": person_id, "name_raw": name, "is_itmo": True,
                                     "publication_ids": ["W1"]}
                                    for person_id, name in (("A1", "Nikolay O. Nikitin"),
                                                            ("A2", "Nikolay Nikitin"))]

                        def fetch_person_authorships_for_dedup(self):
                            return self.rows

                        def fetch_person_publications_for_dedup(self):
                            return [{"id": "W1", "title": "Useful paper", "year": 2025}]

                    class Judge:
                        def __init__(self):
                            self.calls = []

                        def single_many(self, items):
                            self.calls.extend(items)
                            return {context.pair_id: SingleVerdict(Decision.REVIEW, 0.7, "ambiguous")
                                    for context, _ in items}

                    judge = Judge()
                    with self.assertLogs("pauk.graph.dedup", level="WARNING") as logs, patch(
                        "pauk.graph.person_resolution.logistic_probability", return_value=0.5,
                    ):
                        removed, _ = dedup_graph_persons(Client(source_rows), {}, models=judge)
                    self.assertEqual(removed, 0)
                    self.assertTrue(any("person A1" in line for line in logs.output))
                    context, evidence = judge.calls[0]
                    source = full_payload(context, evidence)["source_context"]
                    authorship = source["documents"][0]["a_stored_authorships"][0]
                    self.assertIsNone(authorship["position"])
                    self.assertIsNone(authorship["is_corresponding"])
                    self.assertEqual(authorship["affiliation"], "Physics")
                    affiliations = source["persons"]["a"]["observed_affiliations"]
                    self.assertEqual(affiliations[0]["name"], "ITMO")
                    self.assertEqual(affiliations[0]["years"], [2024, 2025] if isinstance(years, list) else [])
                    self.assertEqual(affiliations[1]["years"], [2023])
                    self.assertEqual(source_rows, before)

    def test_valid_numeric_position_is_retained(self):
        for position in (2, "2"):
            with self.subTest(position=position):
                client = Mock()
                client.fetch_persons_for_dedup.return_value = [{
                    "id": "A", "authorships": [{"publication_id": "W1", "position": position}],
                }]
                client.fetch_person_publications_for_dedup.return_value = []
                with patch("pauk.graph.dedup.plan_person_merges_resolved", return_value=([], [])) as planner:
                    dedup_graph_persons(client, {}, models=Mock())
                self.assertEqual(planner.call_args.args[0][0].authored[0].position, 2)


if __name__ == "__main__":
    unittest.main()
