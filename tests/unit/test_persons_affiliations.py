import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.models import Authorship, Person, Publication
from pauk.pipeline.stages.persons import PersonsStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore

ITMO_INSTITUTION = {"display_name": "ITMO University", "ror": "https://ror.org/04txgxn49"}


def openalex_author(affiliations=(), last_known=(), orcid=None):
    payload = {
        "id": "https://openalex.org/A1", "display_name": "Anonymous Depositor",
        "affiliations": [{"institution": institution, "years": years}
                         for institution, years in affiliations],
        "last_known_institutions": list(last_known),
    }
    if orcid:
        payload["orcid"] = f"https://orcid.org/{orcid}"
    return payload


def orcid_record(name, start=None, end=None, ror=None):
    organization = {"name": name}
    if ror:
        organization["disambiguated-organization"] = {
            "disambiguated-organization-identifier": ror,
            "disambiguation-source": "ROR",
        }
    summary = {"organization": organization}
    if start:
        summary["start-date"] = {"year": {"value": str(start)}}
    if end:
        summary["end-date"] = {"year": {"value": str(end)}}
    return {"activities-summary": {"employments": {"affiliation-group": [
        {"summaries": [{"employment-summary": summary}]},
    ]}}}


class AffiliationBackfillTest(unittest.TestCase):
    def run_stage(self, person, publications, author_payload, orcid_payload=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        raw = RawStore(db, "sample")
        prepared.write_models("persons", [person])
        prepared.write_models("publications", publications)
        with patch("pauk.pipeline.stages.persons.OpenAlexClient") as openalex, \
             patch("pauk.pipeline.stages.persons.OrcidClient") as orcid, \
             patch("pauk.pipeline.stages.persons.CrossrefClient"), \
             patch("pauk.pipeline.stages.persons.OpenReviewClient"):
            openalex.return_value.get_author.return_value = author_payload
            orcid.return_value.get_record.return_value = orcid_payload or {}
            PersonsStage(prepared, raw, Settings(data_dir=root)).run()
        return next(prepared.read_models("persons", Person))

    def test_affiliation_of_the_works_own_year_fills_the_gap(self):
        person = Person(id="A1", openalex_id="A1", is_itmo=False, name_en="Anonymous Depositor",
                        authored=[Authorship(publication_id="W1", position=1),
                                  Authorship(publication_id="W2", position=1)])
        result = self.run_stage(
            person,
            [Publication(id="W1", title="Old work", year=2019),
             Publication(id="W2", title="Recent deposit", year=2026)],
            openalex_author(affiliations=[
                ({"display_name": "Former University", "ror": "https://ror.org/00000000a"}, [2019]),
                (ITMO_INSTITUTION, [2026]),
            ]),
        )
        by_publication = {a.publication_id: a for a in result.authored}
        self.assertEqual(by_publication["W1"].affiliation, "Former University")
        self.assertEqual(by_publication["W2"].affiliation, "ITMO University")
        # The value did not come from the work, and that stays visible.
        self.assertEqual(by_publication["W2"].affiliation_source, "openalex")

    def test_an_affiliation_the_work_states_is_left_alone(self):
        person = Person(id="A1", openalex_id="A1", is_itmo=False,
                        authored=[Authorship(publication_id="W1", position=1,
                                             affiliation="As printed on the paper")])
        result = self.run_stage(
            person, [Publication(id="W1", title="Work", year=2026)],
            openalex_author(affiliations=[(ITMO_INSTITUTION, [2026])]),
        )
        (authorship,) = result.authored
        self.assertEqual(authorship.affiliation, "As printed on the paper")
        self.assertIsNone(authorship.affiliation_source)

    def test_an_itmo_affiliation_no_work_stated_still_marks_the_person(self):
        person = Person(id="A1", openalex_id="A1", is_itmo=False,
                        authored=[Authorship(publication_id="W1", position=1)])
        result = self.run_stage(
            person, [Publication(id="W1", title="Deposit without affiliations", year=2026)],
            openalex_author(affiliations=[(ITMO_INSTITUTION, [2026])]),
        )
        self.assertTrue(result.is_itmo)

    def test_orcid_employment_fills_what_openalex_does_not_know(self):
        person = Person(id="A1", openalex_id="A1", is_itmo=False, orcid="0000-0001",
                        authored=[Authorship(publication_id="W1", position=1)])
        result = self.run_stage(
            person, [Publication(id="W1", title="Work", year=2024)],
            openalex_author(),  # OpenAlex knows no affiliation at all
            orcid_record("University of Nigeria", start=2020),
        )
        (authorship,) = result.authored
        self.assertEqual(authorship.affiliation, "University of Nigeria")
        self.assertEqual(authorship.affiliation_source, "orcid")
        self.assertEqual([a.source for a in result.affiliations], ["orcid"])

    def test_last_known_institution_is_the_fallback_for_an_uncovered_year(self):
        person = Person(id="A1", openalex_id="A1", is_itmo=False,
                        authored=[Authorship(publication_id="W1", position=1)])
        result = self.run_stage(
            person, [Publication(id="W1", title="Work of an unknown year")],
            openalex_author(last_known=[ITMO_INSTITUTION]),
        )
        (authorship,) = result.authored
        self.assertEqual(authorship.affiliation, "ITMO University")

    def test_nothing_to_fill_from_leaves_the_authorship_empty(self):
        person = Person(id="A1", openalex_id="A1", is_itmo=False,
                        authored=[Authorship(publication_id="W1", position=1)])
        result = self.run_stage(
            person, [Publication(id="W1", title="Work", year=2026)], openalex_author())
        (authorship,) = result.authored
        self.assertIsNone(authorship.affiliation)
        self.assertIsNone(authorship.affiliation_source)


if __name__ == "__main__":
    unittest.main()
