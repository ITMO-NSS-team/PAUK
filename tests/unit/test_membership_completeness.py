"""Completeness is an independent owner inventory, not a count of visible rows."""

import json
import unittest
from datetime import UTC, datetime, timedelta

import mongomock

from pauk.graph.client import _person_membership_update
from pauk.graph.extract import extract_node, person_spec
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import merge_nodes
from pauk.graph.unmerge import split_person
from pauk.models import Person, Publication
from pauk.pipeline.membership import (
    ITMO_ROR_ID,
    POLICY_VERSION,
    apply_membership,
    refresh_authorship_membership,
)
from pauk.pipeline.normalize import _merge_person
from pauk.storage import PreparedStore
from tests.unit.test_admin_nodes import FakePanelGraph

OBSERVED = datetime(2026, 11, 1, tzinfo=UTC)


class MembershipCompletenessTest(unittest.TestCase):
    def assessed(self, owners=("A1",), *, legacy_positive=True):
        self.assertTrue("membership_owner_sources" in Person.model_fields, "owner manifest schema is required")
        return Person.model_validate({
            "id": owners[0], "merged_ids": list(owners[1:]), "is_itmo": legacy_positive,
            "name_raw": "Alex Sample", "emails": ["original@example.test"],
            "authored": [{
                "publication_id": "W" + owner[1:], "position": 1,
                "institution_rors": [], "institution_source_person_id": owner,
                "institution_source_id": "W" + owner[1:], "institution_observed_at": OBSERVED,
            } for owner in owners],
            "membership_owner_sources": [{
                "source_person_id": owner, "work_ids": ["W" + owner[1:]], "orcid_ids": [],
                "policy": POLICY_VERSION, "observed_at": OBSERVED,
            } for owner in owners],
        })

    def assert_retained(self, person):
        apply_membership(person)
        self.assertEqual(person.membership_status, "unknown")
        self.assertTrue(person.is_itmo)

    def test_complete_legacy_negative_can_be_reassessed_and_replayed(self):
        person = self.assessed()
        original_authored = [row.model_dump() for row in person.authored]
        for _ in range(3):
            apply_membership(person)
            self.assertEqual(person.membership_status, "unsupported")
            self.assertFalse(person.is_itmo)
        self.assertEqual([row.model_dump() for row in person.authored], original_authored)
        self.assertEqual(person.emails, ["original@example.test"])

    def test_complete_merged_cluster_can_clear_with_all_owner_orcid_observations(self):
        person = self.assessed(("A1", "A2"))
        identifiers = ("0000-0002-1825-0097", "0000-0001-5109-3700")
        for manifest, identifier in zip(person.membership_owner_sources, identifiers, strict=True):
            manifest.orcid_ids = [identifier]
        person = Person.model_validate(person.model_dump() | {"membership_orcid_sources": [
            {"source_person_id": owner, "source_id": identifier, "observed_at": OBSERVED}
            for owner, identifier in zip(("A1", "A2"), identifiers, strict=True)
        ]})
        apply_membership(person)
        self.assertEqual(person.membership_status, "unsupported")
        self.assertFalse(person.is_itmo)
        self.assertEqual(person.merged_ids, ["A2"])
        self.assertEqual({row.institution_source_person_id for row in person.authored}, {"A1", "A2"})

    def test_missing_owner_manifest_retains_merged_uncertainty(self):
        person = self.assessed(("A1", "A2"))
        person.membership_owner_sources = person.membership_owner_sources[:1]
        self.assert_retained(person)

    def test_known_empty_and_unknown_source_inventories_are_distinct(self):
        for field in ("work_ids", "orcid_ids"):
            with self.subTest(field=field):
                person = self.assessed()
                setattr(person.membership_owner_sources[0], field, None)
                self.assert_retained(person)

    def test_expected_work_absent_from_visible_rows_blocks_negative(self):
        person = self.assessed()
        person.membership_owner_sources[0].work_ids.append("W_missing")
        self.assert_retained(person)

    def test_observed_work_outside_manifest_does_not_silently_vanish(self):
        person = self.assessed()
        person.authored.append(person.authored[0].model_copy(update={
            "publication_id": "W_new", "institution_source_id": "W_new", "institution_rors": None,
        }))
        self.assert_retained(person)

    def test_orphan_owner_observation_blocks_negative(self):
        person = self.assessed()
        person.authored.append(person.authored[0].model_copy(update={
            "publication_id": "W3", "institution_source_id": "W3", "institution_source_person_id": "A3",
        }))
        self.assert_retained(person)

    def test_unattributed_source_cannot_be_assigned_to_canonical_owner(self):
        person = self.assessed()
        person.authored[0].institution_source_person_id = None
        self.assert_retained(person)

    def test_missing_expected_orcid_response_blocks_negative(self):
        person = self.assessed()
        person.membership_owner_sources[0].orcid_ids = ["0000-0002-1825-0097"]
        self.assert_retained(person)

    def test_orcid_response_for_other_owner_does_not_cover_current_owner(self):
        person = self.assessed()
        person.membership_owner_sources[0].orcid_ids = ["0000-0002-1825-0097"]
        person = Person.model_validate(person.model_dump() | {"membership_orcid_sources": [{
            "source_id": "0000-0002-1825-0097", "source_person_id": "A2", "observed_at": OBSERVED,
        }]})
        self.assert_retained(person)

    def test_obsolete_manifest_policy_cannot_authorize_negative(self):
        person = self.assessed()
        person.membership_owner_sources[0].policy = "obsolete"
        self.assert_retained(person)

    def test_explicit_empty_source_inventory_can_clear_without_dummy_authorship(self):
        person = self.assessed()
        person.membership_owner_sources[0].work_ids = []
        person.authored = []
        apply_membership(person)
        self.assertFalse(person.is_itmo)
        self.assertEqual(person.membership_status, "unsupported")

    def test_old_or_equal_conflicting_correction_does_not_erase_positive(self):
        for timestamp in (OBSERVED - timedelta(seconds=1), OBSERVED):
            with self.subTest(timestamp=timestamp):
                person = self.assessed()
                row = person.authored[0]
                row.institution_rors = [ITMO_ROR_ID]
                apply_membership(person)
                refresh_authorship_membership(person, row, {"institutions": []}, "A1", "W1", timestamp)
                apply_membership(person)
                self.assertTrue(person.is_itmo)
                self.assertEqual(person.membership_status, "supported")

    def test_merge_preserves_both_independent_owner_inventories(self):
        first, second = self.assessed(), self.assessed(("A2",))
        combined = _merge_person(first, second)
        combined.merged_ids = ["A2"]
        apply_membership(combined)
        self.assertFalse(combined.is_itmo)
        self.assertEqual({row.source_person_id for row in combined.membership_owner_sources}, {"A1", "A2"})
        self.assertEqual({row.institution_source_id for row in combined.authored}, {"W1", "W2"})

    def test_graph_full_owner_manifest_can_retract_legacy_cluster(self):
        person = self.assessed(("A1", "A2"))
        apply_membership(person)
        row = person.model_dump(mode="json")
        _labels, (_identifier, properties) = extract_node(row, person_spec(row))
        result = _person_membership_update({"is_itmo": True, "merged_ids": ["A2"]}, properties, "A1")
        self.assertFalse(result["is_itmo"])
        self.assertEqual(result["membership_status"], "unsupported")

    def test_graph_scope_ids_do_not_substitute_for_missing_owner_manifest(self):
        person = self.assessed(("A1", "A2"))
        apply_membership(person)
        person.membership_owner_sources = person.membership_owner_sources[:1]
        row = person.model_dump(mode="json")
        _labels, (_identifier, properties) = extract_node(row, person_spec(row))
        result = _person_membership_update({"is_itmo": True, "merged_ids": ["A2"]}, properties, "A1")
        self.assertTrue(result["is_itmo"])
        self.assertEqual(result["membership_status"], "unknown")

    def test_partial_canonical_correction_cannot_clear_unknown_merged_owner(self):
        older, newer = OBSERVED.isoformat(), (OBSERVED + timedelta(days=1)).isoformat()
        source = {"source": "openalex_work", "source_person_id": "A1", "source_id": "W1"}
        stored = {
            "id": "A1", "merged_ids": ["A2"], "is_itmo": True,
            "membership_status": "supported", "membership_policy": POLICY_VERSION,
            "membership_evidence": [{**source, "ror": ITMO_ROR_ID, "publication_id": "W1"}],
            "membership_source_coverage": [{**source, "supports_itmo": True, "observed_at": older}],
            "membership_owner_sources": [],
        }
        incoming = {
            "id": "A1", "is_itmo": False, "membership_status": "unsupported",
            "membership_policy": POLICY_VERSION, "membership_evidence": [],
            "membership_source_coverage": [{**source, "supports_itmo": False, "observed_at": newer}],
            "membership_owner_sources": [{
                "source_person_id": "A1", "work_ids": ["W1"], "orcid_ids": [],
                "policy": POLICY_VERSION, "observed_at": newer,
            }],
        }
        result = _person_membership_update(stored, incoming, "A1")
        self.assertTrue(result["is_itmo"])
        self.assertEqual(result["membership_status"], "unknown")
        self.assertEqual(json.loads(result["membership_evidence"]), [])
        coverage = json.loads(result["membership_source_coverage"])
        self.assertEqual(coverage, [{**source, "supports_itmo": False, "observed_at": newer}])

    def test_unmerge_preserves_each_original_inventory_and_source_rows(self):
        database = mongomock.MongoClient()["membership_completeness"]
        store = PreparedStore(database, "sample")
        graph = FakePanelGraph()
        originals = [self.assessed((owner,)) for owner in ("A1", "A2")]
        for person in originals:
            apply_membership(person)
        store.write_models("persons", originals)
        store.write_models("publications", [Publication(id=work, title=work) for work in ("W1", "W2")])
        load_prepared_rows(graph, {filename: list(store.read_rows(entity)) for entity, filename in ENTITY_FILES.items()})
        merge_nodes(graph, "Person", "A2", "A1")
        split_person(graph, database, ["A1", "A2"])
        restored = {row.id: row for row in store.read_models("persons", Person)}
        for original in originals:
            result = restored[original.id]
            self.assertEqual(result.membership_owner_sources, original.membership_owner_sources)
            self.assertEqual(result.authored, original.authored)
            self.assertEqual(result.emails, original.emails)
            self.assertFalse(result.is_itmo)
