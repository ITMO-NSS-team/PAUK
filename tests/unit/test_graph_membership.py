import json
import unittest
from unittest.mock import MagicMock

from pauk.graph.client import (
    Neo4jClient,
    _merge_duplicate_properties,
    _person_membership_update,
)
from pauk.graph.extract import extract_node, extract_relationships, person_spec
from pauk.graph.unmerge import _restore_corresponding
from pauk.pipeline.membership import ITMO_ROR_ID, POLICY_VERSION


def assessed(status, owner="A1"):
    evidence = [{
        "source": "openalex_work", "source_person_id": owner,
        "ror": ITMO_ROR_ID, "publication_id": "W1", "source_id": "W1",
    }] if status == "supported" else []
    return {
        "membership_status": status, "membership_policy": POLICY_VERSION,
        "membership_evidence": json.dumps(evidence), "is_itmo": status == "supported",
    }


def with_coverage(status, work="W1", date="2026-10-29T00:00:00+00:00"):
    properties = assessed(status)
    evidence = json.loads(properties["membership_evidence"])
    for claim in evidence:
        claim.update(publication_id=work, source_id=work)
    properties["membership_evidence"] = json.dumps(evidence)
    properties["membership_source_coverage"] = json.dumps([{
        "source": "openalex_work", "source_person_id": "A1", "source_id": work,
        "observed_at": date, "supports_itmo": status == "supported",
    }])
    return properties


class GraphMembershipTest(unittest.TestCase):
    def test_complete_current_assessment_retracts_legacy_true(self):
        update = _person_membership_update({"is_itmo": True}, assessed("unsupported"), "A1")
        self.assertFalse(update["is_itmo"])
        self.assertEqual(update["membership_status"], "unsupported")

    def test_legacy_false_does_not_retract_true(self):
        self.assertTrue(_person_membership_update({"is_itmo": True}, {"is_itmo": False}, "A1")["is_itmo"])

    def test_obsolete_policy_cannot_retract_true(self):
        incoming = assessed("unsupported") | {"membership_policy": "obsolete"}
        update = _person_membership_update({"is_itmo": True}, incoming, "A1")
        self.assertTrue(update["is_itmo"])
        self.assertEqual(update["membership_status"], "unknown")

    def test_partial_source_cannot_retract_a_live_merged_cluster(self):
        stored = {"is_itmo": True, "merged_ids": ["A2"]}
        update = _person_membership_update(stored, assessed("unsupported"), "A1")
        self.assertTrue(update["is_itmo"])
        self.assertEqual(update["membership_status"], "unknown")

    def test_legacy_import_does_not_resurrect_withdrawn_membership(self):
        update = _person_membership_update(assessed("unsupported"), {"is_itmo": True}, "A1")
        self.assertFalse(update["is_itmo"])
        self.assertEqual(update["membership_status"], "unsupported")

    def test_malformed_evidence_cannot_authorize_reset(self):
        incoming = assessed("unsupported") | {"membership_evidence": "not json"}
        self.assertTrue(_person_membership_update({"is_itmo": True}, incoming, "A1")["is_itmo"])

    def test_fold_preserves_each_original_owner(self):
        update = _merge_duplicate_properties("Person", assessed("supported", "A1"), assessed("supported", "A2"))
        self.assertEqual({item["source_person_id"] for item in json.loads(update["membership_evidence"])}, {"A1", "A2"})
        self.assertEqual(update["membership_status"], "supported")

    def test_fold_unknown_is_not_a_completed_negative(self):
        update = _merge_duplicate_properties("Person", assessed("unsupported"), {"is_itmo": False})
        self.assertEqual(update["membership_status"], "unknown")

    def test_fold_two_completed_negatives_stays_negative(self):
        update = _merge_duplicate_properties("Person", assessed("unsupported"), assessed("unsupported"))
        self.assertEqual(update["membership_status"], "unsupported")
        self.assertFalse(update["is_itmo"])

    def test_graph_extraction_serializes_evidence_and_work_rors(self):
        row = {"id": "A1", **assessed("supported"), "authored": [{
            "publication_id": "W1", "institution_rors": [ITMO_ROR_ID],
            "institution_source_person_id": "A1", "institution_source_id": "W1",
        }]}
        row["membership_evidence"] = json.loads(row["membership_evidence"])
        spec = person_spec(row)
        _, (_, props) = extract_node(row, spec)
        self.assertEqual(json.loads(props["membership_evidence"]), row["membership_evidence"])
        relationships = extract_relationships(row, spec)
        authored = relationships[("Person", "Publication", "AUTHORED", "id")][0][2]
        self.assertEqual(authored["institution_rors"], [ITMO_ROR_ID])
        self.assertEqual(authored["institution_source_person_id"], "A1")

    def test_actual_upsert_passes_authoritative_false_to_cypher(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        session = client.driver.session.return_value.__enter__.return_value
        tx = MagicMock()
        tx.run.side_effect = [
            [{"id": "A1", "props": {"is_itmo": True}}], MagicMock(),
        ]
        session.execute_write.side_effect = lambda callback: callback(tx)
        client.upsert_person_nodes_batch([("A1", assessed("unsupported"))])
        query = tx.run.call_args.args[0]
        batch = tx.run.call_args.kwargs["batch"]
        self.assertIn("SET n.is_itmo = row.is_itmo", query)
        self.assertFalse(batch[0]["is_itmo"])
        self.assertEqual(batch[0]["properties"]["membership_status"], "unsupported")

    def test_actual_authorship_batch_unions_rors_without_inventing_one_owner(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        session = client.driver.session.return_value.__enter__.return_value
        tx = MagicMock()
        tx.run.return_value.single.return_value = {"matched": 2}
        session.execute_write.side_effect = lambda callback: callback(tx)
        client.upsert_relationships_batch("Person", "Publication", "AUTHORED", [
            ("A1", "W1", {"institution_rors": [ITMO_ROR_ID], "institution_source_person_id": "A1"}),
            ("A1", "W1", {"institution_rors": ["012345678"], "institution_source_person_id": "A2"}),
        ])
        props = tx.run.call_args.kwargs["batch"][0]["rel_properties"]
        self.assertEqual(props["institution_rors"], [ITMO_ROR_ID, "012345678"])
        self.assertIsNone(props["institution_source_person_id"])

    def test_partial_republish_cypher_retains_ambiguous_provenance(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        session = client.driver.session.return_value.__enter__.return_value
        tx = MagicMock()
        tx.run.return_value.single.return_value = {"matched": 1}
        session.execute_write.side_effect = lambda callback: callback(tx)
        client.upsert_relationships_batch("Person", "Publication", "AUTHORED", [
            ("A1", "W1", {"institution_rors": [], "institution_source_person_id": "A1"}),
        ])
        query = tx.run.call_args.args[0]
        self.assertIn("was_rors IS NOT NULL OR was_owner IS NOT NULL", query)
        self.assertIn("was_owner IS NULL OR r.institution_source_person_id IS NULL", query)
        self.assertIn("was_source IS NULL OR r.institution_source_id IS NULL", query)

    def test_unmerge_restores_negative_source_on_shared_work(self):
        client = MagicMock()
        key = ("Person", "Publication", "AUTHORED", "id")
        _restore_corresponding(client, "A1", {key: [("A2", "W1", {})]}, {key: [
            ("A1", "W1", {"is_corresponding": False, "institution_rors": [],
                          "institution_source_person_id": "A1", "institution_source_id": "W1"}),
        ]})
        props = client.set_relationship_properties_batch.call_args.args[3][0][2]
        self.assertEqual(props, {
            "is_corresponding": False, "institution_rors": [],
            "institution_source_person_id": "A1", "institution_source_id": "W1",
            "position": None, "affiliation": None, "affiliation_source": None,
        })

    def test_unmerge_keeps_remaining_members_evidence_without_a_fake_owner(self):
        client = MagicMock()
        key = ("Person", "Publication", "AUTHORED", "id")
        _restore_corresponding(client, "A1", {key: [("A2", "W1", {})]}, {key: [
            ("A1", "W1", {"institution_rors": [], "institution_source_person_id": "A1"}),
            ("A1", "W1", {"institution_rors": [ITMO_ROR_ID], "institution_source_person_id": "A3"}),
        ]})
        props = client.set_relationship_properties_batch.call_args.args[3][0][2]
        self.assertEqual(props["institution_rors"], [ITMO_ROR_ID])
        self.assertIsNone(props["institution_source_person_id"])

    def test_unmerge_unknown_legacy_sources_do_not_keep_departed_positive_rors(self):
        client = MagicMock()
        key = ("Person", "Publication", "AUTHORED", "id")
        _restore_corresponding(client, "A1", {key: [("A2", "W1", {})]}, {key: [("A1", "W1", {})]})
        props = client.set_relationship_properties_batch.call_args.args[3][0][2]
        self.assertIsNone(props["institution_rors"])
        self.assertIsNone(props["institution_source_person_id"])
        self.assertIsNone(props["institution_source_id"])

    def test_same_person_partial_work_cannot_retract_other_work_evidence(self):
        stored = with_coverage("supported", "W1")
        incoming = with_coverage("unsupported", "W2", "2026-10-30T00:00:00+00:00")
        result = _person_membership_update(stored, incoming, "A1")
        self.assertTrue(result["is_itmo"])
        self.assertEqual({claim["source_id"] for claim in json.loads(result["membership_evidence"])}, {"W1"})

    def test_newer_same_work_correction_retracts_support(self):
        stored = with_coverage("supported")
        incoming = with_coverage("unsupported", date="2026-10-30T00:00:00+00:00")
        result = _person_membership_update(stored, incoming, "A1")
        self.assertFalse(result["is_itmo"])
        self.assertEqual(json.loads(result["membership_evidence"]), [])

    def test_stale_positive_replay_does_not_resurrect_corrected_source(self):
        stored = with_coverage("unsupported", date="2026-10-30T00:00:00+00:00")
        incoming = with_coverage("supported")
        result = _person_membership_update(stored, incoming, "A1")
        self.assertFalse(result["is_itmo"])
        self.assertEqual(json.loads(result["membership_evidence"]), [])

    def test_equal_time_conflict_does_not_replace_stored_source(self):
        for old, new in (("supported", "unsupported"), ("unsupported", "supported")):
            with self.subTest(stored=old):
                result = _person_membership_update(with_coverage(old), with_coverage(new), "A1")
                self.assertEqual(result["is_itmo"], old == "supported")

    def test_partial_correction_retires_claim_even_when_aggregate_remains_unknown(self):
        stored = with_coverage("supported") | {"merged_ids": ["A2"]}
        incoming = with_coverage("unknown", date="2026-10-30T00:00:00+00:00") | {"is_itmo": True}
        result = _person_membership_update(stored, incoming, "A1")
        self.assertEqual(result["membership_status"], "unknown")
        self.assertEqual(json.loads(result["membership_evidence"]), [])

    def test_work_coverage_is_original_work_not_canonical_target_or_author_position(self):
        row = {"id": "A1", "is_itmo": True, "authored": [{
            "publication_id": "W-canonical", "position": 3,
            "institution_source_person_id": "A1", "institution_source_id": "W-original",
            "institution_rors": [ITMO_ROR_ID], "institution_observed_at": "2026-10-30T00:00:00Z",
        }]}
        _, (_, props) = extract_node(row, person_spec(row))
        coverage = json.loads(props["membership_source_coverage"])
        self.assertEqual(coverage[0]["source_id"], "W-original")
        self.assertNotIn("position", coverage[0])
        self.assertTrue(coverage[0]["supports_itmo"])

    def test_empty_orcid_refresh_keeps_original_owner_in_graph_coverage(self):
        orcid = "0000-0002-1825-0097"
        row = {
            "id": "A1", "merged_ids": ["A2"], "is_itmo": False, "authored": [], "orcid": orcid,
            "affiliations": [], "membership_evidence": [],
            "membership_orcid_sources": [{"source_id": orcid, "source_person_id": "A2",
                                          "observed_at": "2026-10-30T00:00:00Z"}],
            "_processing": {"orcid": {"status": "completed_empty", "request_key": orcid,
                                      "finished_at": "2026-10-30T00:00:00Z"}},
        }
        _, (_, props) = extract_node(row, person_spec(row))
        coverage = json.loads(props["membership_source_coverage"])
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]["source_person_id"], "A2")
        self.assertFalse(coverage[0]["supports_itmo"])
        self.assertNotIn("membership_orcid_sources", props)

    def test_orcid_source_state_from_different_request_does_not_authorize_reset(self):
        row = {
            "id": "A1", "merged_ids": ["A2"], "is_itmo": True, "authored": [], "orcid": "current",
            "membership_orcid_sources": [{"source_id": "old", "source_person_id": "A2"}],
            "_processing": {"orcid": {"status": "completed", "request_key": "current"}},
        }
        _, (_, props) = extract_node(row, person_spec(row))
        self.assertEqual(json.loads(props["membership_source_coverage"]), [])

    def test_folded_publication_keeps_both_original_membership_sources(self):
        original = {
            "publication_id": "W113", "position": 1, "institution_source_person_id": "A1",
            "institution_source_id": "W113", "institution_rors": [],
            "institution_observed_at": "2026-10-29T00:00:00Z",
        }
        duplicate = {**original, "institution_source_id": "W114", "institution_rors": [ITMO_ROR_ID]}
        row = {"id": "A1", "is_itmo": True, "authored": [original],
               "membership_work_sources": [original, duplicate]}
        _, (_, props) = extract_node(row, person_spec(row))
        coverage = {item["source_id"]: item for item in json.loads(props["membership_source_coverage"])}
        self.assertEqual(set(coverage), {"W113", "W114"})
        self.assertFalse(coverage["W113"]["supports_itmo"])
        self.assertTrue(coverage["W114"]["supports_itmo"])
        edges = extract_relationships(row, person_spec(row))
        self.assertEqual(len(edges[("Person", "Publication", "AUTHORED", "id")]), 1)
        self.assertNotIn("membership_work_sources", props)

    def test_source_ledger_correction_wins_over_old_display_authorship(self):
        old_display = {
            "publication_id": "W113", "position": 1, "institution_source_person_id": "A1",
            "institution_source_id": "W114", "institution_rors": [ITMO_ROR_ID],
            "institution_observed_at": "2026-10-29T00:00:00Z",
        }
        corrected = {**old_display, "institution_rors": [], "institution_observed_at": "2026-10-30T00:00:00Z"}
        row = {"id": "A1", **assessed("unsupported"), "authored": [old_display],
               "membership_work_sources": [corrected]}
        _, (_, props) = extract_node(row, person_spec(row))
        result = _person_membership_update(with_coverage("supported", "W114"), props, "A1")
        self.assertFalse(result["is_itmo"])
        self.assertEqual(json.loads(result["membership_evidence"]), [])
        replay = _person_membership_update(result, with_coverage("supported", "W114"), "A1")
        self.assertFalse(replay["is_itmo"])


if __name__ == "__main__":
    unittest.main()
