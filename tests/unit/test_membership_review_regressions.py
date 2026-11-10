"""Membership transitions must preserve uncertainty and original owners."""

import unittest
from datetime import UTC, datetime

import mongomock

from pauk.graph.client import _person_membership_update
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import merge_nodes
from pauk.graph.unmerge import split_person
from pauk.models import Authorship, Person, Publication
from pauk.pipeline.membership import POLICY_VERSION, apply_membership
from pauk.pipeline.normalize import _merge_person
from pauk.storage import PreparedStore
from tests.unit.test_admin_nodes import FakePanelGraph


class MembershipReviewRegressions(unittest.TestCase):
    def negative_person(self):
        person = Person(
            id="A1", is_itmo=False, name_raw="Alex Sample", emails=["a@example.test"],
            authored=[Authorship(
                publication_id="W1", position=1, institution_rors=[],
                institution_source_person_id="A1", institution_source_id="W1",
                institution_observed_at=datetime(2026, 10, 1, tzinfo=UTC),
            )],
            membership_owner_sources=[{
                "source_person_id": "A1", "work_ids": ["W1"], "orcid_ids": [],
                "policy": POLICY_VERSION, "observed_at": datetime(2026, 10, 1, tzinfo=UTC),
            }],
        )
        apply_membership(person)
        self.assertEqual(person.membership_status, "unsupported")
        return person

    def legacy_person(self):
        return Person(id="A2", is_itmo=True, name_raw="A. Sample", emails=["b@example.test"])

    def test_known_negative_work_is_not_a_full_audit_of_legacy_membership(self):
        person = self.negative_person()
        person.is_itmo = True
        person.membership_status = None
        person.membership_owner_sources = []
        for _ in range(3):
            apply_membership(person)
            self.assertEqual(person.membership_status, "unknown")
            self.assertTrue(person.is_itmo)

    def test_partial_raw_coverage_retains_legacy_positive(self):
        person = self.negative_person()
        person.is_itmo = True
        person.membership_status = None
        person.membership_owner_sources = []
        person.authored.append(Authorship(publication_id="W2", institution_rors=None))
        apply_membership(person)
        self.assertEqual(person.membership_status, "unknown")
        self.assertTrue(person.is_itmo)
        self.assertEqual({row.publication_id for row in person.authored}, {"W1", "W2"})

    def test_merge_order_does_not_erase_an_unassessed_positive_owner(self):
        for negative_is_canonical in (True, False):
            with self.subTest(negative_is_canonical=negative_is_canonical):
                negative, legacy = self.negative_person(), self.legacy_person()
                base, extra = (negative, legacy) if negative_is_canonical else (legacy, negative)
                result = _merge_person(base, extra)
                result.merged_ids.append(extra.id)
                apply_membership(result)
                self.assertEqual(set(result.emails), {"a@example.test", "b@example.test"})
                self.assertEqual([row.publication_id for row in result.authored], ["W1"])
                self.assertEqual(result.membership_status, "unknown")
                self.assertTrue(result.is_itmo)

    def test_graph_negative_status_without_source_coverage_cannot_clear_legacy_positive(self):
        incoming = {
            "is_itmo": False, "membership_status": "unsupported",
            "membership_policy": POLICY_VERSION, "membership_evidence": [],
        }
        result = _person_membership_update({"is_itmo": True}, incoming, "A1")
        self.assertTrue(result["is_itmo"])
        self.assertEqual(result["membership_status"], "unknown")

    def test_partial_graph_payload_does_not_clear_an_unseen_owner(self):
        result = _person_membership_update(
            {"is_itmo": True, "merged_ids": ["A2"]},
            {"is_itmo": False, "membership_status": "unsupported",
             "membership_policy": POLICY_VERSION, "membership_evidence": [],
             "membership_source_coverage": [{
                 "source": "openalex_work", "source_person_id": "A1", "source_id": "W1",
                 "observed_at": "2026-10-01T00:00:00+00:00", "supports_itmo": False,
             }]}, "A1",
        )
        self.assertTrue(result["is_itmo"])
        self.assertEqual(result["membership_status"], "unknown")

    def test_graph_fold_and_unmerge_restore_negative_and_unknown_owners(self):
        database = mongomock.MongoClient()["membership_review_regressions"]
        store = PreparedStore(database, "sample")
        graph = FakePanelGraph()
        negative, legacy = self.negative_person(), self.legacy_person()
        store.write_models("persons", [negative, legacy])
        store.write_models("publications", [Publication(id="W1", title="Original work")])
        load_prepared_rows(graph, {filename: list(store.read_rows(entity))
                                   for entity, filename in ENTITY_FILES.items()})
        merge_nodes(graph, "Person", "A2", "A1")
        self.assertTrue(graph.nodes[("Person", "A1")]["is_itmo"])
        split_person(graph, database, ["A1", "A2"])
        self.assertFalse(graph.nodes[("Person", "A1")]["is_itmo"])
        self.assertTrue(graph.nodes[("Person", "A2")]["is_itmo"])
        self.assertEqual(graph.nodes[("Person", "A2")]["membership_status"], "unknown")
        restored = {row.id: row for row in store.read_models("persons", Person)}
        self.assertEqual(restored["A1"].emails, negative.emails)
        self.assertEqual(restored["A2"].emails, legacy.emails)
        self.assertEqual([row.publication_id for row in restored["A1"].authored], ["W1"])
        self.assertEqual(restored["A2"].authored, [])
