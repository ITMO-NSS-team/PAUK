import copy
import unittest
from unittest.mock import patch

from pauk.aliases import MergeAliasConflict
from pauk.graph.person_resolution import Decision
from pauk.graph.person_resolution_single import SingleVerdict
from pauk.models import Person
from pauk.models.person import Authorship
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.publication_identity import publication_identity_map
from pauk.pipeline.stages.dedup import plan_person_merges


class RecordingJudge:
    def __init__(self):
        self.calls = []

    def single_many(self, items):
        self.calls.extend(items)
        return {context.pair_id: SingleVerdict(Decision.MERGE, 0.99, "matching names")
                for context, _ in items}


class PublicationIdentityTest(unittest.TestCase):
    def people(self):
        return [
            Person(id="A", name_raw="Ivan Petrov", is_itmo=True, email="a@example.org",
                   department_ids=["D"], authored=[Authorship(publication_id="W1", position=1)]),
            Person(id="B", name_raw="Ivan Petrov", is_itmo=True, email="b@example.org",
                   department_ids=["D"], authored=[Authorship(publication_id="W2", position=2)]),
            Person(id="C", name_raw="Jane Smith", is_itmo=True, authored=[
                Authorship(publication_id="W1"), Authorship(publication_id="W2"),
            ]),
        ]

    def test_duplicate_work_cannot_supply_independent_coauthor_evidence(self):
        for details in (
            {"W1": {"doi": "https://doi.org/10.1234/ARTICLE"}, "W2": {"doi": "doi:10.1234/article"}},
            {"W2": {"merged_ids": ["W1"]}},
        ):
            with self.subTest(details=details):
                people = self.people()
                original = [person.model_dump() for person in people]
                saved_details = copy.deepcopy(details)
                legacy_groups, _ = plan_person_merges(people, {}, publication_details=details)
                judge = RecordingJudge()
                with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
                    groups, report = plan_person_merges_resolved(people, {}, publication_details=details, models=judge)
                self.assertEqual(legacy_groups, [])
                self.assertEqual(groups, [])
                self.assertTrue(any(row["status"] == "held" for row in report))
                context, evidence = judge.calls[0]
                self.assertEqual(evidence.shared_coauthors, 0)
                self.assertEqual(evidence.small_team_shared_coauthors, 0)
                self.assertEqual(evidence.shared_publications, 1)
                self.assertEqual(context.source_context["nonshared_work_collaborator_paths"], [])
                document = context.source_context["documents"][0]
                self.assertEqual(document["source_record_ids"], ["W1", "W2"])
                self.assertEqual(document["a_stored_authorships"][0]["publication_id"], "W1")
                self.assertEqual(document["b_stored_authorships"][0]["publication_id"], "W2")
                self.assertEqual([person.model_dump() for person in people], original)
                self.assertEqual(details, saved_details)

    def test_independent_works_still_support_merge(self):
        details = {"W1": {"doi": "10.1234/one"}, "W2": {"doi": "10.1234/two"}}
        people = self.people()
        self.assertEqual(len(plan_person_merges(people, {}, publication_details=details)[0]), 1)
        judge = RecordingJudge()
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
            groups, _ = plan_person_merges_resolved(people, {}, publication_details=details, models=judge)
        self.assertEqual(len(groups), 1)
        context, evidence = judge.calls[0]
        self.assertEqual(evidence.small_team_shared_coauthors, 1)
        self.assertEqual(context.source_context["nonshared_work_collaborator_paths"], [
            {"id": "C", "name": "Jane Smith", "a_work_ids": ["W1"], "b_work_ids": ["W2"]},
        ])

    def test_invalid_dois_and_matching_titles_do_not_establish_work_identity(self):
        for doi in (None, "", "unknown", "10.invalid/x", "10.1234/has whitespace", 123):
            with self.subTest(doi=doi):
                details = {key: {"doi": doi, "title": "Same title"} for key in ("W1", "W2")}
                self.assertEqual(publication_identity_map(details, details), {"W1": "W1", "W2": "W2"})

    def test_alias_chains_and_duplicate_dois_are_transitive(self):
        details = {"W2": {"merged_ids": ["W1"], "doi": "10.1234/x"},
                   "W3": {"merged_ids": ["W2"]}, "W4": {"doi": "HTTP://DX.DOI.ORG/10.1234/X"}}
        identities = publication_identity_map(["W1"], details)
        self.assertEqual(set(identities.values()), {"W1"})

    def test_conflicting_or_cyclic_aliases_fail_before_model_call(self):
        for details in (
            {"W2": {"merged_ids": ["W1"]}, "W3": {"merged_ids": ["W1"]}},
            {"W2": {"merged_ids": ["W1"]}, "W1": {"merged_ids": ["W2"]}},
        ):
            with self.subTest(details=details):
                judge = RecordingJudge()
                with self.assertRaises(MergeAliasConflict):
                    plan_person_merges_resolved(self.people(), {}, publication_details=details, models=judge)
                self.assertEqual(judge.calls, [])


if __name__ == "__main__":
    unittest.main()
