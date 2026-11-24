"""Check reassessment query construction; these mocks do not execute Cypher."""

import json
import re
import unittest
from unittest.mock import MagicMock

from pauk.graph.client import Neo4jClient, _person_membership_update
from pauk.pipeline.membership import ITMO_ROR_ID, POLICY_VERSION, normalize_ror


class GraphMembershipReassessmentTest(unittest.TestCase):
    def query(self, rel_type="AUTHORED", properties=None):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        session = client.driver.session.return_value.__enter__.return_value
        tx = MagicMock()
        tx.run.return_value.single.return_value = {"matched": 1}
        session.execute_write.side_effect = lambda callback: callback(tx)
        client.upsert_relationships_batch(
            "Person",
            "Publication",
            rel_type,
            [
                ("A1", "W1", properties or {}),
            ],
        )
        return tx.run.call_args.args[0], tx.run.call_args.kwargs

    def test_clear_requires_current_authoritative_negative_source_person(self):
        query, parameters = self.query(properties={"institution_rors": []})
        self.assertIn("src.membership_policy = $membership_policy", query)
        self.assertIn("src.membership_status = 'unsupported' AND src.is_itmo = false", query)
        self.assertEqual(parameters["membership_policy"], POLICY_VERSION)
        self.assertIn("coalesce(clear_itmo_ror, false)", query)
        self.assertIn("AND r.institution_rors IS NOT NULL", query)
        self.assertIn("ELSE r.institution_rors END", query)

    def test_filter_is_scoped_to_itmo_ror_and_preserves_other_institutions(self):
        query, parameters = self.query()
        pattern = parameters["itmo_ror_pattern"]
        self.assertIn("NOT (trim(toString(value)) =~ $itmo_ror_pattern)", query)
        for ror in (
            ITMO_ROR_ID,
            ITMO_ROR_ID.upper(),
            f"https://ror.org/{ITMO_ROR_ID}",
            f" HTTP://ROR.ORG/{ITMO_ROR_ID.upper()}/ ",
            "012345678",
            "bad-id",
            "prefix04txgxn49",
        ):
            with self.subTest(ror=ror):
                self.assertEqual(bool(re.fullmatch(pattern, ror.strip())), normalize_ror(ror) == ITMO_ROR_ID)

    def test_corresponding_flag_and_partial_publish_union_are_retained(self):
        query, parameters = self.query(properties={"is_corresponding": False, "institution_rors": []})
        self.assertIn("OR coalesce(row.rel_properties.is_corresponding, false)", query)
        self.assertIn("reduce(result = coalesce(was_rors, [])", query)
        self.assertIn("was_owner IS NULL OR r.institution_source_person_id IS NULL", query)
        self.assertEqual(parameters["batch"][0]["rel_properties"]["institution_rors"], [])

    def test_other_relationships_do_not_get_membership_rules(self):
        query, parameters = self.query("OTHER")
        self.assertNotIn("clear_itmo_ror", query)
        self.assertEqual(set(parameters), {"batch"})

    def test_partial_owner_negative_does_not_clear_complete_cluster_membership(self):
        stored = {
            "id": "A1",
            "merged_ids": ["A2"],
            "is_itmo": True,
            "membership_policy": POLICY_VERSION,
            "membership_status": "supported",
            "membership_evidence": [
                {"source": "openalex_work", "source_person_id": "A2", "source_id": "W2", "ror": ITMO_ROR_ID}
            ],
        }
        incoming = {
            "is_itmo": False,
            "membership_status": "unsupported",
            "membership_policy": POLICY_VERSION,
            "membership_evidence": [],
            "membership_source_coverage": [],
            "membership_owner_sources": [
                {"source_person_id": "A1", "policy": POLICY_VERSION, "work_ids": [], "orcid_ids": []}
            ],
        }
        result = _person_membership_update(stored, incoming, "A1")
        self.assertTrue(result["is_itmo"])
        self.assertNotEqual(result["membership_status"], "unsupported")
        self.assertEqual(json.loads(result["membership_evidence"])[0]["source_person_id"], "A2")


if __name__ == "__main__":
    unittest.main()
