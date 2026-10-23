"""Regression coverage for resolver audit and evidence consistency."""

import json
import pickle
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph.person_resolution import (
    MODEL_FEATURES,
    Decision,
    PairEvidence,
    ResearcherContext,
    SecondStageContext,
    feature_vector,
    first_stage_payload,
    resolve_pair,
)
from pauk.graph.person_resolution_model import DEFAULT_MODEL_PATH, load_logistic_model
from pauk.graph.person_resolution_single import SingleVerdict, full_payload
from pauk.models import Person
from pauk.pipeline.person_name_blocking import candidate_person_pairs
from pauk.pipeline.person_resolution import OpenRouterResolutionModels
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.stages.dedup import plan_person_merges
from pauk.settings import Settings
from tests.bench.mocks import RecordingNeo4jClient
from tests.unit.test_mutations import FakeGraph


class ResolverFollowupTest(unittest.TestCase):
    def test_fuzzy_surname_candidate_with_conflicting_initials_needs_no_llm(self):
        people = [
            Person(id="a", name_raw="A. S. Ivanov", is_itmo=True),
            Person(id="b", name_raw="Ivanova, A. K.", is_itmo=True),
        ]

        class Models:
            def single_many(self, items):
                if items:
                    raise AssertionError("Explicit initial conflicts must not call Qwen")
                return {}

        self.assertEqual(len(list(candidate_person_pairs(people, None))), 1)
        groups, report = plan_person_merges_resolved(people, {}, models=Models())
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "separate")
        self.assertEqual(report[0]["reason"], "conflicting initials")

    def test_identifiers_separate_even_when_a_name_contains_several_people(self):
        base = PairEvidence("a", "Ivan Petrov and Petr Ivanov", "b", "Ivan Petrov")
        for fields in (
            {"orcid_a": "0000-0002-1825-0097", "orcid_b": "0000-0001-5109-3700"},
            {"staff_id_a": "1", "staff_id_b": "2"},
        ):
            with self.subTest(fields=fields):
                result = resolve_pair(replace(base, **fields))
                self.assertEqual(result.decision, Decision.SEPARATE)
                self.assertEqual(result.route, "hard_veto")
        self.assertEqual(resolve_pair(base).decision, Decision.REVIEW)
        self.assertEqual(
            resolve_pair(replace(base, orcid_a="0000-0002-1825-0097", orcid_b="0000-0002-1825-0097")).decision, Decision.REVIEW
        )

    def test_legacy_planner_does_not_queue_malformed_identifier_conflicts(self):
        people = [
            Person(id="a", name_raw="Ivan Petrov and Petr Ivanov", orcid="0000-0002-1825-0097", is_itmo=True),
            Person(id="b", name_raw="Ivan Petrov", orcid="0000-0001-5109-3700", is_itmo=True),
        ]
        with patch("pauk.pipeline.stages.dedup._paired_persons", return_value=iter([tuple(people)])):
            groups, report = plan_person_merges(people, {p.id: p.orcid for p in people})
        self.assertEqual(groups, [])
        self.assertEqual(report, [])

    def test_short_complete_surnames_are_not_discarded_as_initials(self):
        for surname in ("Yi", "Ji"):
            for reverse in (False, True):
                with self.subTest(surname=surname, reverse=reverse):
                    name = f"{surname}, Wenlong" if reverse else f"Wenlong {surname}"
                    people = [Person(id="a", name_raw=name, is_itmo=True), Person(id="b", name_raw=f"W. {surname}", is_itmo=True)]
                    pairs = list(candidate_person_pairs(people, None))
                    self.assertEqual([(a.id, b.id) for a, b in pairs], [("a", "b")])
        self.assertEqual(
            list(candidate_person_pairs([Person(id="a", name_raw="A. A.", is_itmo=True), Person(id="b", name_raw="B. A.", is_itmo=True)], None)), []
        )

    def test_qwen_merge_journal_keeps_the_actual_pair_verdict(self):
        people = [Person(id=identifier, name_raw="Nikolay Nikitin", is_itmo=True) for identifier in ("a", "b")]

        class Models:
            def single_many(self, items):
                return {context.pair_id: SingleVerdict(Decision.MERGE, 0.83, "independent evidence") for context, _ in items}

        groups, report = plan_person_merges_resolved(people, {}, models=Models())
        self.assertEqual(len(groups), 1)
        merged = next(row for row in report if row["status"] == "merged")
        self.assertEqual(merged["reason"], "independent evidence")
        self.assertEqual(merged["model_confidence"], 0.83)
        self.assertEqual(merged["model_verdicts"]["single"]["decision"], "merge")
        self.assertEqual(len(merged["merge_evidence"]), 1)

    def test_transitive_merge_records_real_edges_not_an_invented_judgment(self):
        people = [Person(id=identifier, name_raw="Nikolay Nikitin", is_itmo=True) for identifier in ("a", "b", "c")]

        class Models:
            def single_many(self, items):
                return {context.pair_id: SingleVerdict(Decision.MERGE, 0.8, "pair evidence") for context, _ in items}

        with patch("pauk.pipeline.stages.dedup._paired_persons", return_value=iter([(people[0], people[1]), (people[1], people[2])])):
            groups, report = plan_person_merges_resolved(people, {}, models=Models())
        self.assertEqual(len(groups), 1)
        for row in report:
            if row["status"] == "merged":
                edges = {frozenset((e["person_a"], e["person_b"])) for e in row["merge_evidence"]}
                self.assertEqual(edges, {frozenset(("a", "b")), frozenset(("b", "c"))})
                if frozenset((row["person_a"], row["person_b"])) not in edges:
                    self.assertNotIn("model_verdicts", row)

    def test_configured_artifact_controls_the_prompt_and_cache_payload(self):
        evidence = PairEvidence("a", "Nikolay Nikitin", "b", "N. Nikitin")
        context = SecondStageContext(1, ResearcherContext("a", evidence.name_a), ResearcherContext("b", evidence.name_b), "both_missing", "both_missing")
        default = load_logistic_model(expected_features=MODEL_FEATURES)
        custom = replace(default, surname_counts={"nikitin": 1000000})
        expected = round(feature_vector(evidence, custom.surname_counts)["roles_v2__surname_rarity"], 3)
        self.assertNotEqual(expected, first_stage_payload(1, evidence)["pairs"][0]["surname_rarity"])
        self.assertEqual(full_payload(context, evidence, custom)["compact_observations"]["surname_rarity"], expected)
        with tempfile.TemporaryDirectory() as directory:
            payload = pickle.loads(DEFAULT_MODEL_PATH.read_bytes())
            payload["surname_counts"] = dict(custom.surname_counts)
            artifact = Path(directory) / "custom.pkl"
            artifact.write_bytes(pickle.dumps(payload))
            models = OpenRouterResolutionModels(Settings(person_resolution_logreg_model_path=artifact), mongomock.MongoClient().db, "test")
            with patch.object(models, "_run", return_value={}) as run:
                models.single_many([(context, evidence)])
                request = run.call_args.args[0][0]
                self.assertEqual(request.payload["compact_observations"]["surname_rarity"], expected)
                old_request = replace(request, payload=full_payload(context, evidence))
                self.assertNotEqual(models._fingerprint(request), models._fingerprint(old_request))
                json.dumps(request.payload)


class CorrespondingCorrectionTest(unittest.TestCase):
    def test_both_graph_fakes_keep_merges_but_allow_unmerged_corrections(self):
        for factory in (RecordingNeo4jClient, FakeGraph):
            for merged_label in (None, "Person", "Publication"):
                with self.subTest(factory=factory.__name__, merged_label=merged_label):
                    graph = factory()
                    graph.upsert_nodes_batch("Person", [("a", {"id": "a", "merged_ids": ["b"] if merged_label == "Person" else []})])
                    graph.upsert_nodes_batch("Publication", [("w", {"id": "w", "merged_ids": ["v"] if merged_label == "Publication" else []})])
                    for flag in (True, False):
                        graph.upsert_relationships_batch("Person", "Publication", "AUTHORED", [("a", "w", {"is_corresponding": flag})])
                    edges = graph.edges if isinstance(graph, RecordingNeo4jClient) else graph.relationships
                    self.assertEqual(edges[("Person", "AUTHORED", "Publication", "a", "w")]["is_corresponding"], merged_label is not None)
