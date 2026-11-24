import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.aliases import MergeAliasConflict
from pauk.graph.person_resolution import Decision
from pauk.graph.person_resolution_single import SingleVerdict
from pauk.models import Person, Publication, PublicationVersion, VersionAuthor
from pauk.models.person import Authorship
from pauk.pipeline.publication_identity import normalize_publication_doi, publication_identity_map
from pauk.pipeline.stages.dedup import CANDIDATES_FILENAME, DedupStage, _norm_doi
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


class MergeJudge:
    def __init__(self):
        self.calls = []

    def single_many(self, items):
        self.calls.extend(items)
        return {context.pair_id: SingleVerdict(Decision.MERGE, 0.99, "matching names")
                for context, _ in items}


class CrossGroupPublicationIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = mongomock.MongoClient().pauk
        self.current = PreparedStore(self.db, "current")
        self.old = PreparedStore(self.db, "old")
        self.config = Settings(data_dir=Path(self.temporary.name), person_resolution_enabled=False)
        self.stage = DedupStage(self.current, RawStore(self.db, "current"), self.config)

    def people(self, first="W1", second="W2"):
        return [
            Person(id="A", name_raw="Ivan Petrov", is_itmo=True, email="a@example.org",
                   department_ids=["D"], authored=[Authorship(publication_id=first, position=1)]),
            Person(id="B", name_raw="Ivan Petrov", is_itmo=True, email="b@example.org",
                   department_ids=["D"], authored=[Authorship(publication_id=second, position=2)]),
            Person(id="C", name_raw="Jane Smith", is_itmo=True, authored=[
                Authorship(publication_id=first), Authorship(publication_id=second),
            ]),
        ]

    def seed(self, first_doi="10.1234/article", second_doi="https://doi.org/10.1234/article/"):
        self.current.write_models("persons", self.people())
        self.current.write_models("publications", [Publication(id="W1", title="First record", doi=first_doi, year=2018)])
        self.old.write_models("publications", [Publication(id="W2", title="Second record", doi=second_doi, year=2024)])

    def report(self):
        path = self.config.audit_dir / "current" / CANDIDATES_FILENAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_same_work_across_groups_cannot_supply_independent_evidence(self):
        self.seed()
        publications_before = list(self.db.publications.find())
        people_before = list(self.current.read_rows("persons"))
        with patch.object(self.current, "get_models", wraps=self.current.get_models) as fetch:
            removed, held, _ = self.stage._dedup_persons()
        self.assertEqual(removed, 0)
        self.assertGreaterEqual(held, 1)
        self.assertEqual(list(self.current.read_rows("persons")), people_before)
        self.assertEqual(list(self.db.publications.find()), publications_before)
        fetch.assert_called_once_with("publications", ["W1", "W2"], Publication, include_merged=True)
        self.assertTrue(any(row["status"] == "held" and {row["person_a"], row["person_b"]} == {"A", "B"}
                            for row in self.report()))

    def test_resolver_sees_the_old_group_doi_even_when_judge_requests_merge(self):
        self.seed()
        self.stage.config = replace(self.config, person_resolution_enabled=True)
        judge = MergeJudge()
        with (
            patch("pauk.pipeline.person_resolution.ResolutionModels", return_value=judge),
            patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5),
        ):
            removed, held, _ = self.stage._dedup_persons()
        self.assertEqual(removed, 0)
        self.assertGreaterEqual(held, 1)
        context, evidence = next((context, evidence) for context, evidence in judge.calls
                                 if {evidence.person_a, evidence.person_b} == {"A", "B"})
        self.assertEqual(evidence.shared_publications, 1)
        self.assertEqual(evidence.shared_coauthors, 0)
        self.assertEqual(context.source_context["nonshared_work_collaborator_paths"], [])
        self.assertEqual(set(row["id"] for row in self.current.read_rows("persons")), {"A", "B", "C"})

    def test_independent_works_across_groups_still_support_a_merge(self):
        self.seed(second_doi="10.1234/another-work")
        publications_before = list(self.db.publications.find())
        removed, _, _ = self.stage._dedup_persons()
        self.assertEqual(removed, 1)
        self.assertEqual(list(self.db.publications.find()), publications_before)
        people = list(self.current.read_models("persons", Person))
        merged = next(person for person in people if person.id in {"A", "B"})
        self.assertEqual({merged.id, *merged.merged_ids}, {"A", "B"})
        self.assertEqual({row.publication_id for row in merged.authored}, {"W1", "W2"})
        self.assertTrue({"a@example.org", "b@example.org"} <= {merged.email, *merged.emails})

    def test_full_run_preserves_original_author_years_after_publication_fold(self):
        self.current.write_models("persons", self.people())
        self.current.write_models("publications", [
            Publication(id="W1", title="Old source", doi="10.1234/article", year=2018,
                        publication_date="2018-01-01"),
            Publication(id="W2", title="New source", doi="https://doi.org/10.1234/article/", year=2024,
                        publication_date="2024-01-01"),
        ])
        self.stage.config = replace(self.config, person_resolution_enabled=True)
        judge = MergeJudge()
        with (
            patch("pauk.pipeline.person_resolution.ResolutionModels", return_value=judge),
            patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5),
        ):
            result = self.stage.run()
        self.assertEqual(result["dedup_publications_merged"], 1)
        self.assertEqual(result["dedup_merged"], 0)
        context, evidence = next((context, evidence) for context, evidence in judge.calls
                                 if {evidence.person_a, evidence.person_b} == {"A", "B"})
        expected = {"A": [2018, 2018], "B": [2024, 2024]}
        for side, person_id in (("a", evidence.person_a), ("b", evidence.person_b)):
            self.assertEqual(context.source_context["persons"][side]["observed_work_year_range"], expected[person_id])
        (survivor,) = self.current.read_models("publications", Publication)
        self.assertEqual({version.openalex_id: version.year for version in survivor.versions},
                         {"W1": 2018, "W2": 2024})
        saved = survivor.model_dump()
        judge.calls.clear()
        with (
            patch("pauk.pipeline.person_resolution.ResolutionModels", return_value=judge),
            patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5),
        ):
            repeated = self.stage.run()
        self.assertEqual(repeated["dedup_publications_merged"], 0)
        (survivor,) = self.current.read_models("publications", Publication)
        self.assertEqual(survivor.model_dump(), saved)
        context, evidence = next((context, evidence) for context, evidence in judge.calls
                                 if {evidence.person_a, evidence.person_b} == {"A", "B"})
        for side, person_id in (("a", evidence.person_a), ("b", evidence.person_b)):
            self.assertEqual(context.source_context["persons"][side]["observed_work_year_range"], expected[person_id])

    def test_merged_alias_in_old_group_is_resolved_without_restoring_deleted_row(self):
        self.current.write_models("persons", self.people())
        self.old.write_models("publications", [Publication(id="W2", title="Survivor", merged_ids=["W1"])])
        publications_before = list(self.db.publications.find())
        removed, held, _ = self.stage._dedup_persons()
        self.assertEqual(removed, 0)
        self.assertGreaterEqual(held, 1)
        self.assertEqual(list(self.db.publications.find()), publications_before)
        self.assertIsNone(self.db.publications.find_one({"id": "W1"}))

    def test_missing_version_author_does_not_inherit_the_canonical_year(self):
        people = self.people("W2", "W2")
        people[0].merged_ids = ["A-old"]
        self.current.write_models("persons", people)
        self.old.write_models("publications", [Publication(
            id="W2", title="Merged work", year=2024, merged_ids=["W1"], versions=[
                PublicationVersion(openalex_id="W1", year=2018, authors=[VersionAuthor(person_id="A-old")]),
                PublicationVersion(openalex_id="W2", year=2024),
            ],
        )])
        before = copy.deepcopy(list(self.db.publications.find()))
        self.stage.config = replace(self.config, person_resolution_enabled=True)
        judge = MergeJudge()
        with (
            patch("pauk.pipeline.person_resolution.ResolutionModels", return_value=judge),
            patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5),
        ):
            removed, _, _ = self.stage._dedup_persons()
        self.assertEqual(removed, 0)
        context, evidence = next((context, evidence) for context, evidence in judge.calls
                                 if {evidence.person_a, evidence.person_b} == {"A", "B"})
        expected = {"A": [2018, 2018], "B": []}
        for side, person_id in (("a", evidence.person_a), ("b", evidence.person_b)):
            self.assertEqual(context.source_context["persons"][side]["observed_work_year_range"], expected[person_id])
        self.assertEqual(list(self.db.publications.find()), before)
        self.current.upsert_models("publications", list(self.old.read_models("publications", Publication)))
        judge.calls.clear()
        with (
            patch("pauk.pipeline.person_resolution.ResolutionModels", return_value=judge),
            patch("pauk.graph.person_resolution.logistic_probability", return_value=0.5),
        ):
            self.stage.run()
        context, evidence = next((context, evidence) for context, evidence in judge.calls
                                 if {evidence.person_a, evidence.person_b} == {"A", "B"})
        for side, person_id in (("a", evidence.person_a), ("b", evidence.person_b)):
            self.assertEqual(context.source_context["persons"][side]["observed_work_year_range"], expected[person_id])
        (survivor,) = self.current.read_models("publications", Publication)
        self.assertEqual(next(version for version in survivor.versions if version.openalex_id == "W2").authors, [])

    def test_canonical_version_without_year_does_not_borrow_aggregate_year(self):
        self.current.write_models("persons", self.people("W2", "W2"))
        self.old.write_models("publications", [Publication(
            id="W2", title="Merged work", year=2024, merged_ids=["W1"], versions=[
                PublicationVersion(openalex_id="W1", year=2024, authors=[VersionAuthor(person_id="B")]),
                PublicationVersion(openalex_id="W2", year=None, authors=[VersionAuthor(person_id="A")]),
            ],
        )])
        with patch("pauk.pipeline.stages.dedup.plan_person_merges", return_value=([], [])) as planner:
            self.stage._dedup_persons()
        details = planner.call_args.kwargs["publication_details"]
        self.assertIsNone(details["W2"]["year"])
        self.assertEqual(details["W1"]["year"], 2024)

    def test_transitive_alias_chain_is_loaded_beyond_direct_authorship_ids(self):
        self.current.write_models("persons", self.people("W1", "W4"))
        self.old.write_models("publications", [
            Publication(id="W2", title="Middle", merged_ids=["W1"]),
            Publication(id="W3", title="Survivor", merged_ids=["W2"], doi="10.1234/x"),
            Publication(id="W4", title="Other source", doi="10.1234/x"),
        ])
        with patch.object(self.current, "get_models", wraps=self.current.get_models) as fetch:
            removed, held, _ = self.stage._dedup_persons()
        self.assertEqual(removed, 0)
        self.assertGreaterEqual(held, 1)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [["W1", "W4"], ["W2"], ["W3"]])

    def test_conflicting_cross_group_aliases_fail_before_person_writes(self):
        self.current.write_models("persons", self.people("W1", "W4"))
        self.old.write_models("publications", [
            Publication(id="W2", title="First survivor", merged_ids=["W1"]),
            Publication(id="W3", title="Second survivor", merged_ids=["W1"]),
            Publication(id="W4", title="Other"),
        ])
        before = list(self.current.read_rows("persons"))
        with self.assertRaises(MergeAliasConflict):
            self.stage._dedup_persons()
        self.assertEqual(list(self.current.read_rows("persons")), before)
        self.assertEqual(self.db.person_merge_sources.count_documents({}), 0)

    def test_context_only_loads_referenced_works_without_expanding_group(self):
        self.seed()
        self.db.publications.insert_one({"id": "unrelated-corrupt", "groups": ["old"], "year": "not a year"})
        groups_before = {row["id"]: row["groups"] for row in self.db.publications.find()}
        self.stage._dedup_persons()
        self.assertEqual({row["id"]: row["groups"] for row in self.db.publications.find()}, groups_before)

    def test_version_year_is_exposed_only_for_its_confirmed_source_alias(self):
        self.current.write_models("persons", self.people("W-old", "W-new"))
        self.old.write_models("publications", [Publication(
            id="W-new", title="Survivor", year=2024, merged_ids=["W-old", "W-unknown"],
            versions=[
                PublicationVersion(openalex_id="W-old", title="Original", year=2018, doi="10.1234/x"),
                PublicationVersion(openalex_id="W-unconfirmed", year=1999),
            ],
        )])
        with patch("pauk.pipeline.stages.dedup.plan_person_merges", return_value=([], [])) as planner:
            self.stage._dedup_persons()
        details = planner.call_args.kwargs["publication_details"]
        self.assertEqual(details["W-old"]["year"], 2018)
        self.assertEqual(details["W-new"]["year"], 2024)
        self.assertNotIn("W-unknown", details)
        self.assertNotIn("W-unconfirmed", details)

    def test_live_source_record_takes_precedence_over_stale_version_ledger(self):
        self.current.write_models("persons", self.people("W1", "W2"))
        self.old.write_models("publications", [
            Publication(id="W1", title="Live", year=2019),
            Publication(id="W2", title="Survivor", year=2024, merged_ids=["W1"],
                        versions=[PublicationVersion(openalex_id="W1", title="Old ledger", year=2018)]),
        ])
        with patch("pauk.pipeline.stages.dedup.plan_person_merges", return_value=([], [])) as planner:
            self.stage._dedup_persons()
        details = planner.call_args.kwargs["publication_details"]
        self.assertEqual(details["W1"]["year"], 2019)
        self.assertEqual(details["W1"]["title"], "Live")
        self.assertNotIn("source_person_ids", details["W1"])

    def test_live_source_own_ledger_wins_in_both_mongo_insertion_orders(self):
        self.current.write_models("persons", self.people())
        rows = [
            Publication(id="W1", title="Live", year=2019, versions=[
                PublicationVersion(openalex_id="W1", year=2019, authors=[VersionAuthor(person_id="A")]),
            ]),
            Publication(id="W2", title="Survivor", year=2024, merged_ids=["W1"], versions=[
                PublicationVersion(openalex_id="W1", year=2018, authors=[VersionAuthor(person_id="B")]),
            ]),
        ]
        for ordered in (rows, list(reversed(rows))):
            with self.subTest(order=[row.id for row in ordered]):
                self.db.publications.delete_many({})
                self.old.write_models("publications", ordered)
                with patch("pauk.pipeline.stages.dedup.plan_person_merges", return_value=([], [])) as planner:
                    self.stage._dedup_persons()
                details = planner.call_args.kwargs["publication_details"]
                self.assertEqual(details["W1"]["year"], 2019)
                self.assertEqual(details["W1"]["source_person_ids"], ["A"])

    def test_duplicate_version_candidates_are_coherent_and_order_independent(self):
        self.current.write_models("persons", self.people("W2", "W2"))
        for source_id in ("W1", "W2"):
            for conflicting in (False, True):
                first = PublicationVersion(openalex_id=source_id, year=2018,
                                           authors=[VersionAuthor(person_id="A"), VersionAuthor(person_id="B")])
                second = PublicationVersion(openalex_id=source_id, year=2024 if conflicting else 2018,
                                            authors=[VersionAuthor(person_id="B"), VersionAuthor(person_id="A"),
                                                     VersionAuthor(person_id="A")])
                for versions in ([first, second], [second, first]):
                    with self.subTest(source_id=source_id, conflicting=conflicting,
                                      years=[version.year for version in versions]):
                        self.old.write_models("publications", [Publication(
                            id="W2", title="Current", year=2030, merged_ids=["W1"], versions=versions,
                        )])
                        before = copy.deepcopy(list(self.db.publications.find()))
                        with patch("pauk.pipeline.stages.dedup.plan_person_merges", return_value=([], [])) as planner:
                            self.stage._dedup_persons()
                        detail = planner.call_args.kwargs["publication_details"][source_id]
                        self.assertEqual(detail["year"], None if conflicting else 2018)
                        self.assertEqual(detail["source_person_ids"], [] if conflicting else ["A", "B"])
                        self.assertEqual(detail.get("source_record_conflict", False), conflicting)
                        self.assertEqual(list(self.db.publications.find()), before)

    def test_conflicting_version_dois_cannot_establish_an_arbitrary_work_identity(self):
        self.current.write_models("persons", self.people("W2", "W3"))
        for first_doi, second_doi, conflicting in (
            ("10.1234/one", "10.1234/two", True),
            ("10.1234/one", "https://doi.org/10.1234/ONE/", False),
            (None, "10.1234/one", False),
        ):
            versions = [PublicationVersion(openalex_id="W1", year=2018, doi=doi,
                                           authors=[VersionAuthor(person_id="A")])
                        for doi in (first_doi, second_doi)]
            for ordered in (versions, list(reversed(versions))):
                with self.subTest(dois=[version.doi for version in ordered]):
                    self.old.write_models("publications", [
                        Publication(id="W2", title="Survivor", merged_ids=["W1"], versions=ordered),
                        Publication(id="W3", title="Third work", doi="10.1234/one"),
                    ])
                    before = copy.deepcopy(list(self.db.publications.find()))
                    with patch("pauk.pipeline.stages.dedup.plan_person_merges", return_value=([], [])) as planner:
                        self.stage._dedup_persons()
                    details = planner.call_args.kwargs["publication_details"]
                    identities = publication_identity_map(details, details)
                    self.assertEqual(details["W1"].get("source_record_conflict", False), conflicting)
                    if conflicting:
                        self.assertIsNone(details["W1"]["doi"])
                        self.assertNotEqual(identities["W2"], identities["W3"])
                    else:
                        self.assertEqual(normalize_publication_doi(details["W1"]["doi"]), "10.1234/one")
                        self.assertEqual(identities["W2"], identities["W3"])
                    self.assertEqual(list(self.db.publications.find()), before)


class SharedDoiNormalizationTest(unittest.TestCase):
    def test_valid_resolver_spellings_use_one_work_identity(self):
        spellings = (
            "10.1234/article", "10.1234/article/", "10.1234/article///", "https://doi.org/10.1234/article/",
            "HTTPS://DX.DOI.ORG/10.1234/ARTICLE/", "doi:10.1234/article/", " 10.1234/ARTICLE/ ",
            "doi: 10.1234/article", "https://doi.org/ 10.1234/article/",
        )
        details = {f"W{i}": {"doi": doi} for i, doi in enumerate(spellings)}
        self.assertEqual(set(publication_identity_map(details, details).values()), {"W0"})
        for doi in spellings:
            with self.subTest(doi=doi):
                self.assertEqual(_norm_doi(doi), "10.1234/article")
                self.assertEqual(_norm_doi(doi), normalize_publication_doi(doi))

    def test_formatting_does_not_turn_invalid_or_different_dois_into_confirmed_identity(self):
        for doi in (None, "", "unknown", "10.invalid/x", "10.1234/has whitespace", 123, "https://doi.org/"):
            with self.subTest(doi=doi):
                details = {key: {"doi": doi} for key in ("W1", "W2")}
                self.assertEqual(publication_identity_map(details, details), {"W1": "W1", "W2": "W2"})
        details = {"W1": {"doi": "10.1234/one"}, "W2": {"doi": "10.1234/two/"}}
        before = copy.deepcopy(details)
        self.assertEqual(publication_identity_map(details, details), {"W1": "W1", "W2": "W2"})
        self.assertEqual(details, before)


if __name__ == "__main__":
    unittest.main()
