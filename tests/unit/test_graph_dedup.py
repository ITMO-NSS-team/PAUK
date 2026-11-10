import unittest

import mongomock

from pauk.graph.dedup import collect_raw_orcids
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


if __name__ == "__main__":
    unittest.main()
