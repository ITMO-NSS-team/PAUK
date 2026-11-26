"""Graph evidence must recognize historical versions of an already merged work."""

import copy
import itertools
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import mongomock

from pauk.graph.client import Neo4jClient
from pauk.graph.dedup import dedup_graph_persons
from pauk.graph.person_resolution import Decision
from pauk.graph.person_resolution_single import SingleVerdict
from pauk.models import Person, Publication, PublicationVersion
from pauk.models.person import Authorship
from pauk.pipeline.stages.dedup import DedupStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


class EagerJudge:
    def __init__(self):
        self.calls = []

    def single_many(self, items):
        self.calls.extend(items)
        return {context.pair_id: SingleVerdict(Decision.MERGE, 0.99, "matching names")
                for context, _ in items}


class RecordingGraph:
    def __init__(self, rows, publications):
        self.rows = rows
        self.publications = publications
        self.updates = []
        self.merges = []

    def fetch_persons_for_dedup(self):
        return self.rows

    def fetch_person_publications_for_dedup(self):
        return self.publications

    def upsert_person_nodes_batch(self, rows):
        self.updates.extend(rows)

    def merge_person_nodes_batch(self, pairs):
        self.merges.extend(pairs)
        return len(pairs)


class GraphVersionIdentityTest(unittest.TestCase):
    def details(self):
        return {
            "W1": {"id": "W1", "doi": "10.1234/new", "merged_ids": ["W0"],
                   "versions": json.dumps([{"openalex_id": "W0", "doi": "10.1234/old"}])},
            "W2": {"id": "W2", "doi": "10.1234/old", "merged_ids": [], "versions": "[]"},
        }

    def rows(self, details):
        return [
            {"id": person_id, "name_raw": name, "is_itmo": True, "email": email,
             "department_ids": ["D"], "publication_ids": works}
            for person_id, name, email, works in (
                ("A", "Ivan Petrov", "a@example.org", ["W1"]),
                ("B", "Ivan Petrov", "b@example.org", ["W2"]),
                ("C", "Jane Smith", None, ["W1", "W2"]),
            )
        ]

    def run_graph(self, details, *, resolved=False, order=(0, 1, 2)):
        rows = self.rows(details)
        rows = [rows[index] for index in order]
        saved = copy.deepcopy(rows)
        publications = list(copy.deepcopy(details).values())
        saved_publications = copy.deepcopy(publications)
        client = RecordingGraph(rows, publications)
        judge = EagerJudge()
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
            removed, report = dedup_graph_persons(client, {}, models=judge if resolved else None)
        self.assertEqual(rows, saved)
        self.assertEqual(publications, saved_publications)
        return removed, report, judge, client

    def assert_same_work(self, details, *, resolved=False, order=(0, 1, 2)):
        removed, report, judge, client = self.run_graph(details, resolved=resolved, order=order)
        self.assertEqual(removed, 0)
        self.assertEqual(client.merges, [])
        self.assertEqual(client.updates, [])
        self.assertTrue(any(row["status"] == "held" and {row["person_a"], row["person_b"]} == {"A", "B"}
                            for row in report))
        if resolved:
            context, evidence = next((context, evidence) for context, evidence in judge.calls
                                     if {evidence.person_a, evidence.person_b} == {"A", "B"})
            self.assertEqual(evidence.shared_publications, 1)
            self.assertEqual(evidence.shared_coauthors, 0)
            self.assertEqual(context.source_context["nonshared_work_collaborator_paths"], [])

    def test_graph_query_fetches_serialized_version_ledger(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        transaction = Mock()
        transaction.run.return_value = []
        session = client.driver.session.return_value.__enter__.return_value
        session.execute_read.side_effect = lambda read: read(transaction)
        self.assertEqual(client.fetch_persons_for_dedup(), [])
        self.assertNotIn("w.versions", transaction.run.call_args.args[0])
        self.assertEqual(client.fetch_person_publications_for_dedup(), [])
        self.assertIn("w.versions AS versions", transaction.run.call_args.args[0])

    def test_historical_doi_blocks_false_independent_coauthor_in_both_modes(self):
        for resolved, order in itertools.product((False, True), itertools.permutations(range(3))):
            with self.subTest(resolved=resolved, order=order):
                self.assert_same_work(self.details(), resolved=resolved, order=order)

    def test_historical_doi_normalization_matches_primary_doi_normalization(self):
        for doi in ("https://doi.org/10.1234/OLD/", "doi: 10.1234/old/", "http://dx.doi.org/10.1234/old"):
            for resolved in (False, True):
                with self.subTest(doi=doi, resolved=resolved):
                    details = self.details()
                    details["W1"]["versions"] = json.dumps([{"openalex_id": "W0", "doi": doi}])
                    self.assert_same_work(details, resolved=resolved)

    def test_unconfirmed_history_does_not_join_distinct_works(self):
        details = self.details()
        details["W1"]["merged_ids"] = []
        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                removed, _, judge, _ = self.run_graph(details, resolved=resolved)
                self.assertEqual(removed, 1)
                if resolved:
                    _, evidence = judge.calls[0]
                    self.assertEqual((evidence.shared_publications, evidence.shared_coauthors), (0, 1))

    def test_distinct_works_still_supply_independent_coauthor(self):
        details = self.details()
        details["W2"]["doi"] = "10.1234/other"
        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                removed, _, _, _ = self.run_graph(details, resolved=resolved)
                self.assertEqual(removed, 1)

    def test_malformed_history_does_not_crash_or_create_identity(self):
        for versions in ("not-json", "{}", "null", "false", 2024, {}, [None, "bad", {"openalex_id": []}],
                         [{"openalex_id": "W0", "doi": {"value": "10.1234/old"}}]):
            for resolved in (False, True):
                with self.subTest(versions=versions, resolved=resolved):
                    details = self.details()
                    details["W1"]["versions"] = versions
                    removed, _, _, _ = self.run_graph(details, resolved=resolved)
                    self.assertEqual(removed, 1)

    def test_bad_neighbor_does_not_hide_a_confirmed_version(self):
        details = self.details()
        details["W1"]["versions"] = json.dumps([
            None, "bad", {"openalex_id": []}, {"openalex_id": "W0", "doi": "10.1234/old"},
        ])
        for resolved in (False, True):
            with self.subTest(resolved=resolved):
                self.assert_same_work(details, resolved=resolved)

    def test_malformed_optional_metadata_does_not_erase_confirmed_doi(self):
        for optional in ({"year": "unknown"}, {"authors": "unknown"}, {"title": 42},
                         {"publication_date": "not-a-date"}, {"pdf_urls": None},
                         {"authors": [{"person_id": "A", "position": "unknown"}]}):
            for resolved in (False, True):
                with self.subTest(optional=optional, resolved=resolved):
                    details = self.details()
                    details["W1"]["versions"] = json.dumps([
                        {"openalex_id": "W0", "doi": "10.1234/old", **optional},
                    ])
                    self.assert_same_work(details, resolved=resolved)

    def test_contradictory_dois_for_one_version_do_not_choose_a_winner(self):
        versions = [{"openalex_id": "W0", "doi": doi} for doi in ("10.1234/old", "10.1234/conflicting")]
        for ordered in (versions, list(reversed(versions))):
            for resolved in (False, True):
                with self.subTest(ordered=ordered, resolved=resolved):
                    details = self.details()
                    details["W1"]["versions"] = json.dumps(ordered)
                    removed, _, judge, _ = self.run_graph(details, resolved=resolved)
                    self.assertEqual(removed, 1)
                    if resolved:
                        _, evidence = judge.calls[0]
                        self.assertEqual((evidence.shared_publications, evidence.shared_coauthors), (0, 1))

    def test_prepared_and_graph_agree_on_historical_doi(self):
        for resolved in (False, True):
            with self.subTest(resolved=resolved), tempfile.TemporaryDirectory() as temporary:
                db = mongomock.MongoClient().pauk
                prepared = PreparedStore(db, "current")
                people = [Person(id=row["id"], name_raw=row["name_raw"], email=row["email"],
                                 is_itmo=True, department_ids=row["department_ids"],
                                 authored=[Authorship(publication_id=work) for work in row["publication_ids"]])
                          for row in self.rows(self.details())]
                prepared.write_models("persons", people)
                prepared.write_models("publications", [
                    Publication(id="W1", title="Current work", doi="10.1234/new", merged_ids=["W0"],
                                versions=[PublicationVersion(openalex_id="W0", doi="10.1234/old")]),
                    Publication(id="W2", title="Older copy", doi="10.1234/old"),
                ])
                config = replace(Settings(data_dir=Path(temporary)), person_resolution_enabled=resolved)
                stage = DedupStage(prepared, RawStore(db, "current"), config)
                judge = EagerJudge()
                with (
                    patch("pauk.pipeline.person_resolution.ResolutionModels", return_value=judge),
                    patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5),
                ):
                    removed, held, _ = stage._dedup_persons()
                self.assertEqual(removed, 0)
                self.assertGreaterEqual(held, 1)
                self.assert_same_work(self.details(), resolved=resolved)

    def test_version_owner_keeps_own_year_in_graph_model_context(self):
        details = self.details()
        details["W1"]["versions"] = json.dumps([
            {"openalex_id": "W0", "doi": "10.1234/old", "year": 2018,
             "authors": [{"person_id": "A", "position": "unknown"}]},
        ])
        rows = self.rows(details)

        class GraphWithAuthorships(RecordingGraph):
            def fetch_person_authorships_for_dedup(self):
                return [{"person_id": row["id"], "authorships": [
                    {"publication_id": work}
                    for work in row["publication_ids"]
                ]} for row in self.rows]

        publications = [{**work, "year": 2030 if work["id"] == "W1" else 2024, "title": "Paper"}
                        for work in details.values()]
        client = GraphWithAuthorships(rows, publications)
        judge = EagerJudge()
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
            removed, _ = dedup_graph_persons(client, {}, models=judge)
        self.assertEqual(removed, 0)
        context, evidence = judge.calls[0]
        years = {"A": [2018, 2018], "B": [2024, 2024]}
        for side, person_id in (("a", evidence.person_a), ("b", evidence.person_b)):
            self.assertEqual(context.source_context["persons"][side]["observed_work_year_range"], years[person_id])


if __name__ == "__main__":
    unittest.main()
