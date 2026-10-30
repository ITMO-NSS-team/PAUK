import unittest
from datetime import UTC, datetime

from pauk.models import Affiliation, Authorship, Person
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.membership import (
    ITMO_ROR_ID,
    POLICY_VERSION,
    apply_membership,
    authorship_institution_rors,
    coalesce_authorships,
    evaluate_membership,
    normalize_ror,
    orcid_source_owner,
    refresh_authorship_membership,
    refresh_orcid_membership,
)


class MembershipTest(unittest.TestCase):
    def test_coalescing_keeps_distinct_original_authors_on_one_publication(self):
        person = Person(id="A1", is_itmo=False, merged_ids=["A2"], authored=[
            Authorship(publication_id="W1", position=1, is_corresponding=True,
                       institution_source_person_id="A1", institution_source_id="W1"),
            Authorship(publication_id="W1", position=1, is_corresponding=False,
                       institution_source_person_id="A2", institution_source_id="W1"),
        ])
        coalesce_authorships(person)
        self.assertEqual([row.is_corresponding for row in person.authored], [True, False])
        self.assertEqual([row.institution_source_person_id for row in person.authored], ["A1", "A2"])
        self.assertEqual({row.institution_source_person_id for row in person.membership_work_sources}, {"A1", "A2"})

    def test_legacy_true_is_unknown_and_retained(self):
        person = Person(id="A1", is_itmo=True)
        self.assertIsNone(person.membership_status)
        apply_membership(person)
        self.assertEqual(person.membership_status, "unknown")
        self.assertEqual(person.membership_policy, POLICY_VERSION)
        self.assertTrue(person.is_itmo)

    def test_profile_and_filled_affiliation_are_not_work_evidence(self):
        person = Person(id="A1", is_itmo=False, affiliations=[
            Affiliation(name="ITMO", ror=ITMO_ROR_ID, source="openalex"),
        ], authored=[Authorship(publication_id="W1", affiliation="ITMO", affiliation_source="openalex")])
        self.assertEqual(evaluate_membership(person).status, "unknown")
        self.assertFalse(evaluate_membership(person).is_itmo)

    def test_orcid_is_independent_historical_evidence(self):
        person = Person(id="A1", is_itmo=False, affiliations=[
            Affiliation(name="ITMO", ror=ITMO_ROR_ID, source="orcid", years=[2001]),
        ])
        result = evaluate_membership(person)
        self.assertEqual(result.status, "supported")
        self.assertEqual(result.evidence[0].source, "orcid")
        self.assertIsNone(result.evidence[0].source_person_id)

    def test_known_empty_is_distinct_from_unknown(self):
        person = Person(id="A1", is_itmo=False, authored=[Authorship(publication_id="W1")])
        self.assertEqual(evaluate_membership(person).status, "unknown")
        person.authored[0].institution_rors = []
        self.assertEqual(evaluate_membership(person).status, "unsupported")

    def test_unqueried_orcid_or_merged_owner_is_not_negative(self):
        person = Person(id="A1", is_itmo=False, orcid="record", authored=[
            Authorship(publication_id="W1", institution_rors=[]),
        ])
        self.assertEqual(evaluate_membership(person).status, "unknown")
        person.processing["orcid"] = ProcessingState(status=ProcessingStatus.COMPLETED, request_key="record")
        self.assertEqual(evaluate_membership(person).status, "unsupported")
        person.merged_ids = ["A2"]
        self.assertEqual(evaluate_membership(person).status, "unknown")

    def test_correction_retracts_only_the_refreshed_source_slot(self):
        row = Authorship(publication_id="W1", position=1)
        person = Person(id="A1", is_itmo=False, authored=[row])
        refresh_authorship_membership(person, row, {"institutions": [{"ror": ITMO_ROR_ID}]}, "A1", "W1")
        apply_membership(person)
        self.assertEqual(person.membership_status, "supported")
        refresh_authorship_membership(person, row, {"institutions": []}, "A1", "W1")
        apply_membership(person)
        self.assertEqual(person.membership_status, "unsupported")
        self.assertFalse(person.is_itmo)
        self.assertEqual(person.membership_evidence, [])

    def test_evaluation_reconciles_current_work_not_stale_evidence(self):
        row = Authorship(publication_id="W1", position=1, institution_rors=[ITMO_ROR_ID],
                         institution_source_person_id="A1", institution_source_id="W1")
        person = Person(id="A1", is_itmo=False, authored=[row])
        apply_membership(person)
        row.institution_rors = []
        apply_membership(person)
        self.assertEqual(person.membership_status, "unsupported")

    def test_partial_missing_payload_preserves_positive_evidence(self):
        row = Authorship(publication_id="W1", position=1)
        person = Person(id="A1", is_itmo=False, authored=[row])
        refresh_authorship_membership(person, row, {"institutions": [{"ror": ITMO_ROR_ID}]}, "A1", "W1")
        apply_membership(person)
        refresh_authorship_membership(person, row, {}, "A1", "W1")
        apply_membership(person)
        self.assertEqual(person.membership_status, "supported")

    def test_correction_does_not_retract_another_owner_or_work(self):
        first = Authorship(publication_id="W1", position=1)
        second = Authorship(publication_id="W1", position=1)
        person = Person(id="A1", is_itmo=False, merged_ids=["A2"], authored=[first, second])
        refresh_authorship_membership(person, first, {"institutions": [{"ror": ITMO_ROR_ID}]}, "A1", "W1")
        refresh_authorship_membership(person, second, {"institutions": [{"ror": ITMO_ROR_ID}]}, "A2", "W2")
        apply_membership(person)
        refresh_authorship_membership(person, first, {"institutions": []}, "A1", "W1")
        apply_membership(person)
        self.assertEqual(person.membership_status, "supported")
        self.assertEqual([(row.source_person_id, row.source_id) for row in person.membership_evidence], [("A2", "W2")])

    def test_orcid_success_supersedes_claims_while_another_owner_survives(self):
        person = Person(id="A1", is_itmo=False, authored=[Authorship(publication_id="W1", institution_rors=[])])
        affiliation = Affiliation(name="ITMO", ror=ITMO_ROR_ID, source="orcid")
        refresh_orcid_membership(person, [affiliation], "A1", "orcid-1")
        apply_membership(person)
        refresh_orcid_membership(person, [], "A1", "orcid-1")
        apply_membership(person)
        self.assertEqual(person.membership_status, "unsupported")
        refresh_orcid_membership(person, [affiliation], "A2", "orcid-2")
        refresh_orcid_membership(person, [], "A1", "orcid-1")
        self.assertEqual(evaluate_membership(person).status, "supported")

    def test_ror_uses_exact_identifier_and_host(self):
        for value in (ITMO_ROR_ID, f"https://ror.org/{ITMO_ROR_ID}/"):
            self.assertEqual(normalize_ror(value), ITMO_ROR_ID)
        for value in (f"https://evil.test/{ITMO_ROR_ID}", f"prefix-{ITMO_ROR_ID}", "ITMO University"):
            self.assertIsNone(normalize_ror(value))
        self.assertIsNone(authorship_institution_rors({"institutions": [{"id": ITMO_ROR_ID}]}))
        self.assertEqual(authorship_institution_rors({"institutions": []}), [])

    def test_orcid_owner_is_not_inferred_from_merged_canonical_id(self):
        person = Person(id="A1", is_itmo=False, merged_ids=["A2"], orcid="record")
        self.assertIsNone(orcid_source_owner(person, "record"))
        affiliation = Affiliation(name="ITMO", ror=ITMO_ROR_ID, source="orcid",
                                  source_person_id="A2", source_id="record")
        person.affiliations = [affiliation]
        self.assertEqual(orcid_source_owner(person, "record"), "A2")
        person.affiliations.append(affiliation.model_copy(update={"source_person_id": "A1"}))
        self.assertIsNone(orcid_source_owner(person, "record"))
        refresh_orcid_membership(person, [affiliation], None, "record")
        self.assertEqual({row.source_person_id for row in person.affiliations}, {"A1", "A2", None})

    def test_unknown_orcid_owner_refresh_cannot_delete_unattributed_legacy_source(self):
        person = Person(id="A1", is_itmo=False, merged_ids=["A2"], affiliations=[
            Affiliation(name="ITMO", ror=ITMO_ROR_ID, source="orcid"),
        ])
        refresh_orcid_membership(person, [], None, "record")
        self.assertEqual(len(person.affiliations), 1)
        self.assertIsNone(person.affiliations[0].source_id)

    def test_empty_orcid_response_retains_owner_and_request_identity(self):
        person = Person(id="A1", is_itmo=False, merged_ids=["A2"], orcid="record")
        affiliation = Affiliation(name="ITMO", ror=ITMO_ROR_ID, source="orcid")
        refresh_orcid_membership(person, [affiliation], "A2", "record")
        apply_membership(person)
        observed_at = datetime(2026, 10, 30, tzinfo=UTC)
        refresh_orcid_membership(person, [], orcid_source_owner(person, "record"), "record", observed_at=observed_at)
        apply_membership(person)
        self.assertEqual(person.membership_evidence, [])
        self.assertEqual(person.affiliations, [])
        self.assertEqual(orcid_source_owner(person, "record"), "A2")
        self.assertIsNone(orcid_source_owner(person, "another-record"))
        self.assertEqual(len(person.membership_orcid_sources), 1)
        self.assertEqual(person.membership_orcid_sources[0].observed_at, observed_at)
        restored = Person.model_validate(person.model_dump(mode="json"))
        self.assertEqual(orcid_source_owner(restored, "record"), "A2")

    def test_orcid_source_state_preserves_distinct_owners_on_merge(self):
        from pauk.pipeline.normalize import _merge_person

        first = Person(id="A1", is_itmo=False, orcid="first")
        second = Person(id="A2", is_itmo=False, orcid="second")
        refresh_orcid_membership(first, [], "A1", "first")
        refresh_orcid_membership(second, [], "A2", "second")
        merged = _merge_person(first, second)
        merged.merged_ids = ["A2"]
        self.assertEqual(orcid_source_owner(merged, "first"), "A1")
        self.assertEqual(orcid_source_owner(merged, "second"), "A2")

    def test_evaluation_is_pure(self):
        person = Person(id="A1", is_itmo=False, authored=[Authorship(publication_id="W1", institution_rors=[ITMO_ROR_ID])])
        original = person.model_dump()
        evaluate_membership(person)
        self.assertEqual(person.model_dump(), original)
