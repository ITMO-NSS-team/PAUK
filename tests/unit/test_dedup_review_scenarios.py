"""Adversarial person-resolution scenarios with a deliberately eager local judge."""

import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph.person_resolution import Decision
from pauk.graph.person_resolution_single import SingleVerdict, full_payload
from pauk.models import Person, Publication, PublicationVersion, VersionAuthor
from pauk.models.person import Authorship
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.stages.dedup import DedupStage, plan_person_merges
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


class EagerRecordingJudge:
    """Expose every payload and test safeguards even when the judge wants to merge."""

    def __init__(self):
        self.calls = []

    def single_many(self, rows):
        self.calls.extend(rows)
        return {context.pair_id: SingleVerdict(Decision.MERGE, 0.99, "same name")
                for context, _ in rows}


class DedupReviewScenariosTest(unittest.TestCase):
    def people(self, first_work="W1", second_work="W2"):
        return [
            Person(id="A", name_raw="Ivan Petrov", is_itmo=True, email="a@example.org",
                   department_ids=["D"], authored=[Authorship(publication_id=first_work, position=1)]),
            Person(id="B", name_raw="Ivan Petrov", is_itmo=True, email="b@example.org",
                   department_ids=["D"], authored=[Authorship(publication_id=second_work, position=2)]),
            Person(id="C", name_raw="Jane Smith", is_itmo=True, authored=[
                Authorship(publication_id=first_work), Authorship(publication_id=second_work),
            ]),
        ]

    def resolve(self, people, details):
        saved_people = [person.model_dump() for person in people]
        saved_details = copy.deepcopy(details)
        judge = EagerRecordingJudge()
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5):
            groups, report = plan_person_merges_resolved(people, {}, publication_details=details, models=judge)
        self.assertEqual(saved_people, [person.model_dump() for person in people])
        self.assertEqual(saved_details, details)
        self.assertEqual(len(judge.calls), 1)
        context, evidence = judge.calls[0]
        payload = full_payload(context, evidence)
        json.dumps(payload, allow_nan=False)
        self.assertEqual(payload["source_context"], context.source_context)
        return groups, report, context.source_context, evidence

    def test_doi_url_spelling_cannot_make_one_paper_independent_evidence(self):
        for spelling in (
            "10.1234/article/", "https://doi.org/10.1234/article/",
            "http://dx.doi.org/10.1234/ARTICLE/", "doi:10.1234/article/",
            "doi: 10.1234/article/", "https://doi.org/ 10.1234/article/",
            " https://doi.org/10.1234/article/ ",
        ):
            with self.subTest(spelling=spelling):
                details = {"W1": {"doi": "10.1234/article"}, "W2": {"doi": spelling}}
                people = self.people()
                self.assertEqual(plan_person_merges(people, {}, publication_details=details)[0], [])
                groups, report, source, evidence = self.resolve(people, details)
                self.assertEqual(groups, [])
                self.assertTrue(any(row["status"] == "held" for row in report))
                self.assertEqual((evidence.shared_publications, evidence.shared_coauthors), (1, 0))
                self.assertEqual(source["nonshared_work_collaborator_paths"], [])

    def test_original_author_years_survive_record_order_and_id_order(self):
        for first_id, second_id in (("W1", "W2"), ("W2", "W1")):
            for reversed_input in (False, True):
                with self.subTest(ids=(first_id, second_id), reversed_input=reversed_input):
                    people = self.people(first_id, second_id)
                    rows = [(first_id, {"doi": "10.1234/same", "year": 2018}),
                            (second_id, {"doi": "10.1234/same", "year": 2024})]
                    if reversed_input:
                        rows.reverse()
                    _, _, source, _ = self.resolve(people, dict(rows))
                    self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [2018, 2018])
                    self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [2024, 2024])
                    document = source["documents"][0]
                    self.assertIsNone(document["year"])
                    self.assertTrue(document["year_conflict"])
                    observed = {row["publication_id"]: row["year"] for row in document["source_record_years"]}
                    self.assertEqual(observed, {first_id: 2018, second_id: 2024})
                    self.assertEqual(document["a_stored_authorships"][0]["publication_year"], 2018)
                    self.assertEqual(document["b_stored_authorships"][0]["publication_year"], 2024)

    def test_missing_alias_source_does_not_borrow_the_survivors_year(self):
        details = {"W2": {"merged_ids": ["W1"], "year": 2024}}
        groups, _, source, evidence = self.resolve(self.people(), details)
        self.assertEqual(groups, [])
        self.assertEqual(evidence.shared_publications, 1)
        self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [])
        self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [])
        document = source["documents"][0]
        self.assertIsNone(document["a_stored_authorships"][0]["publication_year"])
        self.assertIsNone(document["b_stored_authorships"][0]["publication_year"])

    def test_person_order_does_not_transfer_years_between_sides(self):
        details = {"W1": {"doi": "10.1234/same", "year": 2018},
                   "W2": {"doi": "10.1234/same", "year": 2024}}
        for people in itertools.permutations(self.people()):
            with self.subTest(order=[person.id for person in people]):
                _, _, source, evidence = self.resolve(list(people), details)
                years = {"A": [2018, 2018], "B": [2024, 2024]}
                self.assertEqual(source["persons"]["a"]["observed_work_year_range"], years[evidence.person_a])
                self.assertEqual(source["persons"]["b"]["observed_work_year_range"], years[evidence.person_b])

    def test_canonical_authorships_use_original_version_owners(self):
        details = {
            "W1": {"year": 2018, "source_person_ids": ["A"]},
            "W2": {"year": 2024, "merged_ids": ["W1"], "source_person_ids": ["B"]},
        }
        groups, _, source, evidence = self.resolve(self.people("W2", "W2"), details)
        self.assertEqual(groups, [])
        self.assertEqual(evidence.shared_publications, 1)
        self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [2018, 2018])
        self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [2024, 2024])
        self.assertTrue(source["documents"][0]["year_conflict"])

    def test_folded_work_with_missing_ownership_does_not_invent_person_years(self):
        details = {"W1": {"year": 2018}, "W2": {"year": 2024, "merged_ids": ["W1"]}}
        _, _, source, _ = self.resolve(self.people("W2", "W2"), details)
        for side in ("a", "b"):
            self.assertEqual(source["persons"][side]["observed_work_year_range"], [])
        self.assertTrue(source["documents"][0]["year_conflict"])

    def test_original_version_owner_matches_a_persons_preserved_alias(self):
        people = self.people("W2", "W2")
        people[0].merged_ids = ["A-original"]
        details = {
            "W1": {"year": 2018, "source_person_ids": ["A-original"]},
            "W2": {"year": 2024, "merged_ids": ["W1"], "source_person_ids": ["B"]},
        }
        _, _, source, _ = self.resolve(people, details)
        self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [2018, 2018])
        self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [2024, 2024])

    def test_partial_version_ownership_leaves_only_the_unidentified_person_unknown(self):
        details = {
            "W1": {"year": 2018, "source_person_ids": ["A"]},
            "W2": {"year": 2024, "merged_ids": ["W1"]},
        }
        _, _, source, _ = self.resolve(self.people("W2", "W2"), details)
        self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [2018, 2018])
        self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [])

    def test_invalid_years_are_unknown_not_borrowed_or_coerced(self):
        for invalid in (None, 0, -1, True, "2018", [2018]):
            with self.subTest(invalid=invalid):
                details = {"W1": {"doi": "10.1234/same", "year": invalid},
                           "W2": {"doi": "10.1234/same", "year": 2024}}
                _, _, source, _ = self.resolve(self.people(), details)
                self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [])
                self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [2024, 2024])
                self.assertIsNone(source["documents"][0]["a_stored_authorships"][0]["publication_year"])

    def test_all_original_authorships_contribute_to_each_person_range(self):
        people = self.people()
        people[0].authored += [Authorship(publication_id="W3"), Authorship(publication_id="W3")]
        details = {"W1": {"doi": "10.1234/same", "year": 2018},
                   "W2": {"doi": "10.1234/same", "year": 2024},
                   "W3": {"doi": "10.1234/same", "year": 2020}}
        groups, _, source, evidence = self.resolve(people, details)
        self.assertEqual(groups, [])
        self.assertEqual((evidence.shared_publications, evidence.shared_coauthors), (1, 0))
        self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [2018, 2020])
        self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [2024, 2024])

    def test_unshown_alias_still_exposes_conflicting_document_year(self):
        details = {f"W{i:02}": {"doi": "10.1234/same", "year": 2018} for i in range(12)}
        details["W11"]["year"] = 2024
        _, _, source, _ = self.resolve(self.people("W00", "W11"), details)
        document = source["documents"][0]
        self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [2024, 2024])
        self.assertTrue(document["year_conflict"])
        self.assertIsNone(document["year"])
        self.assertLessEqual(len(document["source_record_years"]), 10)
        self.assertEqual(document["source_records_available"], 12)

    def test_consistent_years_keep_the_single_document_year(self):
        details = {work_id: {"doi": "10.1234/same", "year": 2020} for work_id in ("W1", "W2")}
        _, _, source, _ = self.resolve(self.people(), details)
        self.assertEqual(source["documents"][0]["year"], 2020)
        self.assertFalse(source["documents"][0]["year_conflict"])
        for side in ("a", "b"):
            self.assertEqual(source["persons"][side]["observed_work_year_range"], [2020, 2020])

    def test_genuinely_distinct_works_keep_the_independent_corroboration(self):
        details = {"W1": {"doi": "10.1234/first/", "year": 2018},
                   "W2": {"doi": "10.1234/second/", "year": 2024}}
        people = self.people()
        self.assertEqual(len(plan_person_merges(people, {}, publication_details=details)[0]), 1)
        groups, _, source, evidence = self.resolve(people, details)
        self.assertEqual(len(groups), 1)
        self.assertEqual((evidence.shared_publications, evidence.shared_coauthors), (0, 1))
        self.assertEqual(len(source["nonshared_work_collaborator_paths"]), 1)
        self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [2018, 2018])
        self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [2024, 2024])


class ConflictingHistoricalLedgerScenariosTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = mongomock.MongoClient().pauk
        self.current = PreparedStore(self.db, "current")
        self.old = PreparedStore(self.db, "old")
        self.config = Settings(data_dir=Path(self.temporary.name), person_resolution_enabled=True)
        self.stage = DedupStage(self.current, RawStore(self.db, "current"), self.config)
        self.current.write_models("persons", DedupReviewScenariosTest().people("W2", "W3"))

    def exercise_ledgers(self, *, conflicting, reverse):
        rows = [
            Publication(id="W2", title="Intermediate", merged_ids=["W1"], versions=[
                PublicationVersion(openalex_id="W1", year=2018, authors=[VersionAuthor(person_id="A")]),
            ]),
            Publication(id="W3", title="Final", merged_ids=["W1", "W2"], versions=[
                PublicationVersion(openalex_id="W1", year=2020 if conflicting else 2018,
                                   authors=[VersionAuthor(person_id="B" if conflicting else "A")]),
            ]),
        ]
        self.db.publications.delete_many({})
        self.old.write_models("publications", list(reversed(rows)) if reverse else rows)
        publications_before = copy.deepcopy(list(self.db.publications.find()))
        people_before = copy.deepcopy(list(self.current.read_rows("persons")))
        judge = EagerRecordingJudge()
        observed = {}

        def capture(*args, **kwargs):
            observed.update(copy.deepcopy(kwargs["publication_details"]))
            return plan_person_merges_resolved(*args, **kwargs)

        with (
            patch("pauk.pipeline.person_resolution.ResolutionModels", return_value=judge),
            patch("pauk.pipeline.person_resolution_planner.plan_person_merges_resolved", side_effect=capture),
            patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5),
        ):
            removed, held, _ = self.stage._dedup_persons()
        self.assertEqual(removed, 0)
        self.assertGreater(held, 0)
        self.assertEqual(list(self.db.publications.find()), publications_before)
        self.assertEqual(list(self.current.read_rows("persons")), people_before)
        (context, evidence), = judge.calls
        self.assertEqual((evidence.shared_publications, evidence.shared_coauthors), (1, 0))
        json.dumps(full_payload(context, evidence), allow_nan=False)
        return observed, context.source_context

    def test_overlapping_ledgers_cannot_mix_the_year_of_one_with_the_owner_of_another(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                details, source = self.exercise_ledgers(conflicting=True, reverse=reverse)
                self.assertIsNone(details["W1"]["year"])
                self.assertEqual(details["W1"]["source_person_ids"], [])
                self.assertTrue(details["W1"]["source_record_conflict"])
                for side in ("a", "b"):
                    self.assertEqual(source["persons"][side]["observed_work_year_range"], [])
                document = source["documents"][0]
                self.assertIsNone(document["year"])
                self.assertTrue(document["source_record_conflict"])

    def test_agreeing_overlapping_ledgers_retain_the_confirmed_year_and_owner(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                details, source = self.exercise_ledgers(conflicting=False, reverse=reverse)
                self.assertEqual(details["W1"]["year"], 2018)
                self.assertEqual(details["W1"]["source_person_ids"], ["A"])
                self.assertFalse(details["W1"].get("source_record_conflict", False))
                self.assertEqual(source["persons"]["a"]["observed_work_year_range"], [2018, 2018])
                self.assertEqual(source["persons"]["b"]["observed_work_year_range"], [])


if __name__ == "__main__":
    unittest.main()
