import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph.person_resolution import (
    Decision,
    PairEvidence,
    ResearcherContext,
    Resolution,
    SecondStageContext,
    feature_vector,
    merge_review_reason,
    resolve_pair,
)
from pauk.graph.person_resolution_single import SingleVerdict, apply_verdict, full_payload, parse_response
from pauk.models import Person
from pauk.pipeline.person_resolution import OpenRouterResolutionModels
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.stages.dedup import DedupStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore, review


class SingleModels:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def single_many(self, items):
        self.calls.extend(items)
        return {context.pair_id: self.verdict for context, _ in items}

    def first_many(self, items):
        raise AssertionError("single mode must not call the legacy first judge")

    def second_many(self, items):
        raise AssertionError("single mode must not call the legacy second judge")


def evidence(**changes):
    return replace(PairEvidence("A1", "Nikolay O. Nikitin", "A2", "Nikolay Nikitin"), **changes)


class SingleResolutionTest(unittest.TestCase):
    def test_all_three_actions_are_explicit(self):
        for action in ("merge", "separate", "review"):
            with self.subTest(action=action):
                answer = parse_response({"id": 7, "decision": action, "confidence": 0.4, "reason": "evidence"}, 7)
                self.assertEqual(answer.decision, action)

    def test_invalid_answers_are_rejected(self):
        valid = {"id": 7, "decision": "review", "confidence": 0.8, "reason": "uncertain"}
        for changes in (
            {"id": True},
            {"id": 7.0},
            {"id": 8},
            {"decision": "yes"},
            {"confidence": True},
            {"confidence": math.nan},
            {"confidence": math.inf},
            {"confidence": -0.1},
            {"confidence": 1.1},
            {"reason": " "},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_response(valid | changes, 7)

    def test_payload_has_no_prior_verdict_or_score(self):
        context = SecondStageContext(
            7, ResearcherContext("A1", "Nikitin"), ResearcherContext("A2", "Nikitin"), "both_missing", "both_missing"
        )
        payload = full_payload(context, evidence())
        self.assertTrue({"first_stage", "logreg_probability", "impact_not_identity_evidence"}.isdisjoint(payload))
        self.assertIn("compact_observations", payload)
        self.assertIn("catalog_status", payload["a"])

    def test_matching_identifier_cannot_override_unrelated_primary_names(self):
        for identity in ({"orcid_a": "0000-1", "orcid_b": "0000-1"}, {"staff_id_a": "42", "staff_id_b": "42"}):
            with self.subTest(identity=identity):
                pair = evidence(name_a="Oles Prokhorchuk", name_b="Maria Tanasa", **identity)
                self.assertEqual(resolve_pair(pair).decision, Decision.REVIEW)
                answer = apply_verdict(
                    Resolution(Decision.FIRST_MODEL, "qwen_first", 0.7),
                    pair,
                    SingleVerdict(Decision.MERGE, 1.0, "same identifier"),
                )
                self.assertEqual(answer.decision, Decision.REVIEW)

    def test_compatible_identifier_spelling_is_not_blocked(self):
        pair = evidence(name_a="Yu. V. Generalova", name_b="Yulia Generalova", orcid_a="0000-1", orcid_b="0000-1")
        self.assertIsNone(merge_review_reason(pair))
        self.assertEqual(resolve_pair(pair).decision, Decision.MERGE)

    def test_initials_without_independent_support_defer_both_merge_sources(self):
        pair = evidence(name_a="A. V. Ivanov", name_b="A. V. Ivanov", small_team_shared_coauthors=0)
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.999):
            self.assertEqual(resolve_pair(pair).decision, Decision.REVIEW)
        answer = apply_verdict(
            Resolution(Decision.FIRST_MODEL, "qwen_first", 0.7),
            pair,
            SingleVerdict(Decision.MERGE, 0.99, "matching initials"),
        )
        self.assertEqual(answer.decision, Decision.REVIEW)

    def test_unknown_support_is_not_zero_support(self):
        pair = evidence(name_a="A. V. Ivanov", name_b="A. V. Ivanov")
        self.assertIsNone(merge_review_reason(pair))

    def test_trusted_id_and_small_team_support_are_independent_exceptions(self):
        pair = evidence(name_a="A. V. Ivanov", name_b="A. V. Ivanov", small_team_shared_coauthors=0)
        self.assertIsNone(merge_review_reason(replace(pair, staff_id_a="42", staff_id_b="42")))
        self.assertIsNone(merge_review_reason(replace(pair, shared_coauthors=1, small_team_shared_coauthors=1)))

    def test_guard_never_changes_separation_into_merge(self):
        pair = evidence(name_a="A. V. Ivanov", name_b="A. V. Ivanov", small_team_shared_coauthors=0)
        answer = apply_verdict(
            Resolution(Decision.FIRST_MODEL, "qwen_first", 0.5),
            pair,
            SingleVerdict(Decision.SEPARATE, 0.9, "explicit conflicting values"),
        )
        self.assertEqual(answer.decision, Decision.SEPARATE)

    def test_roles_use_surname_not_shared_given_name(self):
        values = feature_vector(evidence(name_a="Sergey Ivanov", name_b="Sergey Petrov"))
        self.assertEqual(values["roles_v2__surname_equal"], 0)
        reordered = feature_vector(evidence(name_a="Valentin Malykh", name_b="Malykh, Valentin"))
        self.assertEqual(reordered["roles_v2__surname_equal"], 1)

    def test_planner_uses_one_judge_and_persists_review_verdict(self):
        people = [
            Person(id="A1", name_raw="Nikolay O. Nikitin", is_itmo=True),
            Person(id="A2", name_raw="Nikolay Nikitin", is_itmo=True),
        ]
        for action in (Decision.MERGE, Decision.SEPARATE, Decision.REVIEW):
            with self.subTest(action=action):
                models = SingleModels(SingleVerdict(action, 0.8, "supplied evidence"))
                groups, report = plan_person_merges_resolved(people, {}, models=models)
                self.assertEqual(len(models.calls), 1)
                self.assertIsNone(models.calls[0][0].first_verdict)
                if action is Decision.MERGE:
                    self.assertEqual(len(groups), 1)
                    self.assertEqual(report[-1]["route"], "qwen_single")
                else:
                    self.assertEqual(groups, [])
                    self.assertEqual(set(report[0]["model_verdicts"]), {"single"})
                    self.assertEqual(report[0]["model_verdicts"]["single"]["decision"], action)
                    db = mongomock.MongoClient().db
                    review.record_held(db, report, source=review.STAGE)
                    self.assertEqual(len(review.questions(db)), int(action is Decision.REVIEW))

    def test_unavailable_single_model_leaves_review_not_a_fake_vote(self):
        people = [
            Person(id="A1", name_raw="Nikolay O. Nikitin", is_itmo=True),
            Person(id="A2", name_raw="Nikolay Nikitin", is_itmo=True),
        ]
        groups, report = plan_person_merges_resolved(people, {}, models=SingleModels(None))
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["model_verdicts"], {"single": None})
        self.assertEqual(report[0]["status"], "held")

    def test_high_probability_namesake_uses_judge_and_waits_if_unavailable(self):
        people = [
            Person(id="A1", name_raw="Sergey Makarov", is_itmo=True),
            Person(id="A2", name_raw="Sergey Makarov", is_itmo=True),
        ]
        models = SingleModels(None)
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.999):
            groups, report = plan_person_merges_resolved(people, {}, models=models)
        self.assertEqual(len(models.calls), 1)
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "held")
        db = mongomock.MongoClient().db
        review.record_held(db, report, source=review.STAGE)
        self.assertEqual(len(review.questions(db)), 1)

    def test_joint_work_only_reaches_judge_and_waits_if_unavailable(self):
        people = [
            Person(id="A1", name_raw="Huadong Wang", is_itmo=True, authored=[{"publication_id": "W1"}]),
            Person(id="A2", name_raw="Huadong Wang", is_itmo=True, authored=[{"publication_id": "W1"}]),
            Person(id="A3", name_raw="Jane Smith", is_itmo=True, authored=[{"publication_id": "W1"}]),
        ]
        models = SingleModels(None)
        with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.999):
            groups, report = plan_person_merges_resolved(people, {}, models=models)
        self.assertEqual(len(models.calls), 1)
        _, pair = models.calls[0]
        self.assertEqual(pair.shared_publications, 1)
        self.assertEqual(pair.shared_coauthors, 0)
        self.assertEqual(pair.small_team_shared_coauthors, 0)
        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "held")
        db = mongomock.MongoClient().db
        review.record_held(db, report, source=review.STAGE)
        self.assertEqual(len(review.questions(db)), 1)

    def test_single_response_is_cached_across_pair_renumbering(self):
        models = OpenRouterResolutionModels(
            Settings(openrouter_api_key="test-key"), mongomock.MongoClient().db, "test"
        )
        context = SecondStageContext(
            7, ResearcherContext("A1", "Nikitin"), ResearcherContext("A2", "Nikitin"), "both_missing", "both_missing"
        )
        requests = []

        def respond(client, prompt, **kwargs):
            self.assertEqual(kwargs["max_tokens"], 260)
            requests.append(prompt)
            return {"id": json.loads(prompt)["id"], "decision": "review", "confidence": 0.8, "reason": "namesakes"}

        with patch("pauk.sources.OpenRouterClient.chat_json", respond):
            first = models.single_many([(context, evidence())])
            second = models.single_many([(replace(context, pair_id=99), evidence())])
        self.assertEqual(len(requests), 1)
        self.assertEqual(first[7], second[99])
        self.assertEqual(first[7].decision, Decision.REVIEW)

    def test_actual_stage_sends_one_request_and_persists_each_action(self):
        for action in ("merge", "separate", "review"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                db = mongomock.MongoClient().db
                prepared, raw = PreparedStore(db, "sample"), RawStore(db, "sample")
                prepared.write_models(
                    "persons",
                    [
                        Person(id="A1", name_raw="Nikolay O. Nikitin", is_itmo=True),
                        Person(id="A2", name_raw="Nikolay Nikitin", is_itmo=True),
                    ],
                )
                config = Settings(
                    data_dir=Path(directory), openrouter_api_key="test-key", person_resolution_enabled=True
                )
                requests = []

                def respond(client, prompt, action=action, requests=requests, **kwargs):
                    payload = json.loads(prompt)
                    requests.append(payload)
                    return {"id": payload["id"], "decision": action, "confidence": 0.8, "reason": "supplied evidence"}

                with patch("pauk.sources.OpenRouterClient.chat_json", respond):
                    result = DedupStage(prepared, raw, config).run()
                self.assertEqual(len(requests), 1)
                self.assertEqual(result["dedup_merged"], int(action == "merge"))
                self.assertEqual(result["dedup_candidates"], int(action == "review"))
                questions = review.questions(db)
                self.assertEqual(len(questions), int(action == "review"))
                if questions:
                    self.assertEqual(questions[0]["evidence"]["model_verdicts"]["single"]["decision"], "review")
