import json
import unittest
from unittest.mock import patch

import mongomock

from pauk.graph.dedup import collect_raw_orcids, dedup_graph_persons
from pauk.graph.person_resolution import Decision
from pauk.graph.person_resolution_single import SingleVerdict, full_payload
from pauk.models import Person
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.stages.dedup import plan_person_merges
from pauk.storage import RawStore


class CollectRawOrcidsTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_later_fetch_overrides_earlier_across_groups(self):
        RawStore(self.db, "group-a").append(
            "openalex_authors",
            {"id": "https://openalex.org/A1", "orcid": "https://orcid.org/0000-0002-1825-0097"}, {})
        RawStore(self.db, "group-b").append(
            "openalex_authors",
            {"id": "https://openalex.org/A1", "orcid": "https://orcid.org/0000-0001-5109-3700"}, {})
        orcids = collect_raw_orcids(self.db)
        self.assertEqual(orcids["A1"], "0000-0001-5109-3700")

    def test_foreign_url_cannot_become_a_trusted_orcid(self):
        identifier = "0000-0002-1825-0097"
        foreign_url = f"https://example.org/{identifier}"
        for author, orcid in (("A1", f"http://orcid.org/{identifier}/"), ("A2", foreign_url)):
            RawStore(self.db, "group-a").append(
                "openalex_authors", {"id": f"https://openalex.org/{author}", "orcid": orcid}, {})
        orcids = collect_raw_orcids(self.db)
        self.assertEqual(orcids["A1"], identifier)
        self.assertEqual(orcids["A2"], foreign_url)
        people = [Person(id=author, name_raw="Ivan Petrov", is_itmo=True) for author in ("A1", "A2")]
        groups, report = plan_person_merges_resolved(people, orcids)
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "held")
        self.assertEqual(report[0]["route"], "invalid_orcid_reference")

    def test_missing_orcid_is_none(self):
        RawStore(self.db, "group-a").append(
            "openalex_authors", {"id": "https://openalex.org/A1"}, {})
        orcids = collect_raw_orcids(self.db)
        self.assertIsNone(orcids["A1"])

    def test_matching_foreign_references_are_not_trusted_by_either_planner(self):
        foreign_url = "https://example.org/0000-0002-1825-0097"
        for author in ("A1", "A2"):
            RawStore(self.db, "group-a").append(
                "openalex_authors", {"id": f"https://openalex.org/{author}", "orcid": foreign_url}, {})
        orcids = collect_raw_orcids(self.db)
        people = [Person(id=author, name_raw="Ivan Petrov", is_itmo=True) for author in ("A1", "A2")]
        for planner in (plan_person_merges_resolved, plan_person_merges):
            with self.subTest(planner=planner.__name__):
                groups, report = planner(people, orcids)
                self.assertEqual(groups, [])
                self.assertEqual(report[0]["status"], "held")
                self.assertEqual(report[0]["route"], "invalid_orcid_reference")

    def test_matching_malformed_bare_ids_are_held_by_both_planners(self):
        people = [Person(id=author, name_raw="Ivan Petrov", is_itmo=True) for author in ("A1", "A2")]
        for invalid_id in ("bogus", "0000-0001"):
            for planner in (plan_person_merges_resolved, plan_person_merges):
                with self.subTest(invalid_id=invalid_id, planner=planner.__name__):
                    groups, report = planner(people, {person.id: invalid_id for person in people})
                    self.assertEqual(groups, [])
                    self.assertEqual(report[0]["status"], "held")
                    self.assertEqual(report[0]["route"], "invalid_orcid_reference")

    def test_ignores_other_sources(self):
        RawStore(self.db, "group-a").append(
            "openalex_works", {"id": "https://openalex.org/W1"}, {})
        self.assertEqual(collect_raw_orcids(self.db), {})


class GraphSourceContextTest(unittest.TestCase):
    def test_rule_only_graph_pass_does_not_fetch_expensive_source_context(self):
        class Client:
            def fetch_persons_for_dedup(self):
                return [{"id": "A1", "name_raw": "Solo Person", "is_itmo": True}]

            def fetch_person_authorships_for_dedup(self):
                raise AssertionError("context query must not run in rule-only mode")

            def fetch_publication_fields(self):
                return {}

        removed, report = dedup_graph_persons(Client(), {})
        self.assertEqual((removed, report), (0, []))

    def test_graph_authorship_positions_and_affiliations_reach_judge(self):
        class Client:
            def fetch_persons_for_dedup(self):
                return [
                    {"id": person_id, "name_raw": name, "is_itmo": True,
                     "publication_ids": ["W1"]}
                    for person_id, name in (
                        ("A1", "Nikolay O. Nikitin"), ("A2", "Nikolay Nikitin")
                    )
                ]

            def fetch_person_authorships_for_dedup(self):
                return [
                    {
                        "person_id": person_id,
                        "affiliations": json.dumps([{"name": affiliation, "source": "openalex"}]),
                        "authorships": [{
                            "publication_id": "W1", "position": position,
                            "affiliation": affiliation, "affiliation_source": "openalex",
                            "is_corresponding": False, "doi": "10.123/test",
                            "title": "Shared paper", "year": 2025,
                        }],
                    }
                    for person_id, position, affiliation in (
                        ("A1", 2, "Lab A"),
                        ("A2", 2, "Lab B"),
                    )
                ]

            def fetch_publication_fields(self):
                return {}

        class Judge:
            def __init__(self):
                self.calls = []

            def single_many(self, items):
                self.calls.extend(items)
                return {context.pair_id: SingleVerdict(Decision.REVIEW, 0.7, "ambiguous")
                        for context, _ in items}

        judge = Judge()
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
            removed, _ = dedup_graph_persons(Client(), {}, models=judge)

        self.assertEqual(removed, 0)
        self.assertEqual(len(judge.calls), 1)
        context, evidence = judge.calls[0]
        source = full_payload(context, evidence)["source_context"]
        document = source["documents"][0]
        self.assertEqual(document["a_stored_authorships"][0]["position"], 2)
        self.assertEqual(document["b_stored_authorships"][0]["position"], 2)
        self.assertEqual(document["a_stored_authorships"][0]["affiliation"], "Lab A")
        self.assertEqual(document["b_stored_authorships"][0]["affiliation"], "Lab B")
        self.assertIsNone(document["a_stored_authorships"][0]["is_corresponding"])
        self.assertEqual(source["persons"]["a"]["observed_affiliations"][0]["name"], "Lab A")
        self.assertIn("graph records", source["basis"])

    def test_legacy_graph_rows_do_not_invent_first_author_position(self):
        class Client:
            def fetch_persons_for_dedup(self):
                return [
                    {"id": person_id, "name_raw": name, "is_itmo": True,
                     "publication_ids": ["W1"]}
                    for person_id, name in (
                        ("A1", "Nikolay O. Nikitin"), ("A2", "Nikolay Nikitin")
                    )
                ]

            def fetch_publication_fields(self):
                return {}

        class Judge:
            def __init__(self):
                self.calls = []

            def single_many(self, items):
                self.calls.extend(items)
                return {context.pair_id: SingleVerdict(Decision.REVIEW, 0.7, "ambiguous")
                        for context, _ in items}

        judge = Judge()
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
            removed, _ = dedup_graph_persons(Client(), {}, models=judge)

        self.assertEqual(removed, 0)
        self.assertEqual(len(judge.calls), 1)
        self.assertIsNone(judge.calls[0][0].source_context)


if __name__ == "__main__":
    unittest.main()
