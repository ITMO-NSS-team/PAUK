"""Identity normalization and short-surname safety at the planner boundary."""

import unittest
from dataclasses import replace

from pauk.graph.person_resolution import (
    Decision,
    PairEvidence,
    _normalize_orcid,
    feature_vector,
    first_stage_payload,
    resolve_pair,
    shared_identity_facts,
)
from pauk.models import Person
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.stages.dedup import _group_conflict, _partition_conflicted_component, plan_person_merges


class OrcidCanonicalizationTest(unittest.TestCase):
    identifier = "0000-0002-1825-0097"

    def test_accepted_forms_share_identity(self):
        for variant in (
            self.identifier, f"https://orcid.org/{self.identifier}",
            f"https://orcid.org/{self.identifier}/", f"http://orcid.org/{self.identifier}/",
            f" HTTPS://ORCID.ORG/{self.identifier}/ ",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(_normalize_orcid(variant), self.identifier)
                result = resolve_pair(PairEvidence(
                    "a", "Ivan Petrov", "b", "Ivan Petrov", orcid_a=self.identifier, orcid_b=variant,
                ))
                self.assertEqual(result.decision, Decision.MERGE)
                self.assertEqual(result.route, "same_orcid")

    def test_malformed_references_are_not_identity_or_conflict_evidence(self):
        for variant in (
            f"https://example.org/{self.identifier}",
            f"https://orcid.org.evil.test/{self.identifier}",
            f"https://orcid.org/{self.identifier}?other=1", "orcid.org/0000-0002-1825-0097",
        ):
            with self.subTest(variant=variant):
                self.assertNotEqual(_normalize_orcid(variant), self.identifier)
                result = resolve_pair(PairEvidence(
                    "a", "Ivan Petrov", "b", "Ivan Petrov", orcid_a=self.identifier, orcid_b=variant,
                ))
                self.assertEqual(result.decision, Decision.REVIEW)
                self.assertEqual(result.route, "invalid_orcid_reference")

    def test_distinct_valid_ids_still_separate(self):
        result = resolve_pair(PairEvidence(
            "a", "Ivan Petrov", "b", "Ivan Petrov",
            orcid_a=self.identifier, orcid_b="0000-0001-5109-3700",
        ))
        self.assertEqual(result.decision, Decision.SEPARATE)
        self.assertEqual(result.reason, "conflicting ORCID")

    def test_distinct_malformed_values_do_not_create_a_group_conflict(self):
        people = {key: Person(id=key, name_raw="Ivan Petrov", is_itmo=True) for key in ("a", "b")}
        identities = {"a": "bogus", "b": "https://example.org/0000-0002-1825-0097"}
        self.assertIsNone(_group_conflict(list(people), people, identities))
        for planner in (plan_person_merges_resolved, plan_person_merges):
            with self.subTest(planner=planner.__name__):
                groups, report = planner(list(people.values()), identities)
                self.assertEqual(groups, [])
                self.assertEqual(report[0]["status"], "held")
                self.assertEqual(report[0]["route"], "invalid_orcid_reference")

    def test_identical_unverified_references_cannot_auto_merge(self):
        for value in (
            f"https://example.org/{self.identifier}", f"orcid.org/{self.identifier}",
            f"https://orcid.org/{self.identifier}?other=1", f"person/{self.identifier}",
            "bogus", "0000-0001",
        ):
            with self.subTest(value=value):
                item = PairEvidence("a", "Ivan Petrov", "b", "Ivan Petrov", orcid_a=value, orcid_b=value)
                result = resolve_pair(item)
                self.assertEqual(result.decision, Decision.REVIEW)
                self.assertEqual(result.route, "invalid_orcid_reference")
                self.assertEqual(shared_identity_facts(item)["orcid_relation"], "unverified")
                self.assertEqual(first_stage_payload(1, item)["pairs"][0]["orcid"], "unverified")

    def test_planners_and_component_checks_agree(self):
        people = [Person(id="a", name_raw="Ivan Petrov", orcid=self.identifier, is_itmo=True),
                  Person(id="b", name_raw="Ivan Petrov", orcid=f"http://orcid.org/{self.identifier}/", is_itmo=True)]
        identities = {person.id: person.orcid for person in people}
        original = dict(identities)
        by_id = {person.id: person for person in people}
        self.assertIsNone(_group_conflict(["a", "b"], by_id, identities))
        for planner in (plan_person_merges_resolved, plan_person_merges):
            with self.subTest(planner=planner.__name__):
                groups, report = planner(people, identities)
                self.assertEqual(len(groups), 1)
                self.assertFalse(any(row["status"] == "held" for row in report))
        self.assertEqual(identities, original)

    def test_component_partition_preserves_equivalent_identifiers(self):
        people = {identifier: Person(id=identifier, name_raw="Ivan Petrov", is_itmo=True)
                  for identifier in ("a", "b", "c")}
        identities = {"a": self.identifier, "b": f"http://orcid.org/{self.identifier}/",
                      "c": "0000-0001-5109-3700"}
        parts = _partition_conflicted_component(["a", "b", "c"], [("a", "b"), ("b", "c")], people, identities, {})
        self.assertEqual(parts, [["a", "b"], ["c"]])


class ShortSurnameSafetyTest(unittest.TestCase):
    def test_role_loss_is_deferred_without_changing_fitted_features(self):
        for surname in ("Yi", "Ji"):
            for name in (f"Wenlong {surname}", f"{surname}, Wenlong"):
                with self.subTest(name=name):
                    item = PairEvidence("a", name, "b", f"W. {surname}")
                    vector = feature_vector(item)
                    self.assertEqual(vector["roles_v2__roles_missing"], 1)
                    result = resolve_pair(item)
                    self.assertEqual(result.decision, Decision.FIRST_MODEL)
                    self.assertEqual(result.route, "short_surname_role_loss")
                    self.assertLess(result.probability, 0.13)
                    self.assertEqual(feature_vector(item), vector)
                    people = [Person(id="a", name_raw=name, is_itmo=True),
                              Person(id="b", name_raw=f"W. {surname}", is_itmo=True)]
                    groups, report = plan_person_merges_resolved(people, {})
                    self.assertEqual(groups, [])
                    self.assertEqual(report[0]["status"], "held")

    def test_clear_conflicts_are_not_rescued(self):
        base = PairEvidence("a", "Wenlong Yi", "b", "W. Yi")
        for item in (
            replace(base, initials_conflict=True),
            replace(base, orcid_a="0000-0002-1825-0097", orcid_b="0000-0001-5109-3700"),
            replace(base, name_b="P. Yi"), replace(base, name_b="W. Ji"),
        ):
            with self.subTest(item=item):
                result = resolve_pair(item)
                self.assertNotEqual(result.route, "short_surname_role_loss")
                self.assertNotEqual(result.decision, Decision.MERGE)

    def test_short_guard_does_not_override_a_matching_identifier(self):
        result = resolve_pair(PairEvidence(
            "a", "Wenlong Yi", "b", "W. Yi",
            orcid_a="0000-0002-1825-0097", orcid_b="0000-0002-1825-0097",
        ))
        self.assertEqual(result.decision, Decision.MERGE)
        self.assertEqual(result.route, "same_orcid")
