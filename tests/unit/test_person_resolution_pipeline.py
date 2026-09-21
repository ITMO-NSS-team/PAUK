import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph.person_resolution import (
    ModelVerdict,
    PairEvidence,
    first_stage_payload,
)
from pauk.models import Person
from pauk.pipeline.person_resolution import (
    OpenRouterResolutionModels,
    _Request,
    _Result,
)
from pauk.pipeline.person_resolution_planner import (
    DIFFERENT,
    SAME,
    plan_person_merges_resolved,
)
from pauk.pipeline.stages.dedup import DedupStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore, review


def person(person_id: str, name: str, *, orcid: str | None = None) -> Person:
    return Person(
        id=person_id,
        openalex_id=person_id,
        is_itmo=True,
        name_raw=name,
        orcid=orcid,
    )


class FakeModels:
    def __init__(self, first=None, second=None):
        self.first = first
        self.second = second
        self.first_calls = []
        self.second_calls = []

    def first_many(self, items):
        self.first_calls.extend(items)
        return {pair_id: self.first for pair_id, _ in items}

    def second_many(self, contexts):
        self.second_calls.extend(contexts)
        return {context.pair_id: self.second for context in contexts}


class PersonResolutionPipelineTest(unittest.TestCase):
    def setUp(self):
        self.people = [
            person("A1", "Nikolay O. Nikitin"),
            person("A2", "Nikolay Nikitin"),
        ]

    def test_two_positive_models_merge_the_pair(self):
        models = FakeModels(
            ModelVerdict(True, 0.9, "compatible names"),
            ModelVerdict(True, 0.85, "corroborated identity"),
        )

        groups, report = plan_person_merges_resolved(self.people, {}, models=models)

        self.assertEqual(len(groups), 1)
        self.assertEqual({groups[0][0].id, groups[0][1][0].id}, {"A1", "A2"})
        self.assertEqual(report[-1]["status"], "merged")
        self.assertEqual(report[-1]["rules"], ["qwen_second_merge"])
        self.assertEqual(len(models.first_calls), 1)
        self.assertEqual(len(models.second_calls), 1)

    def test_first_model_rejection_becomes_a_review_question(self):
        models = FakeModels(ModelVerdict(False, 0.8, "insufficient evidence"))

        groups, report = plan_person_merges_resolved(self.people, {}, models=models)

        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "held")
        self.assertEqual(report[0]["route"], "qwen_first_separate")
        self.assertEqual(models.second_calls, [])

    def test_model_failure_is_safe_and_reviewable(self):
        groups, report = plan_person_merges_resolved(
            self.people, {}, models=FakeModels()
        )

        self.assertEqual(groups, [])
        self.assertEqual(report[0]["held_because"], ["first model unavailable"])

    def test_a_human_merge_answer_skips_both_models(self):
        models = FakeModels()

        groups, report = plan_person_merges_resolved(
            self.people,
            {},
            decisions={frozenset(("A1", "A2")): SAME},
            models=models,
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(report[-1]["rules"], ["manual"])
        self.assertEqual(models.first_calls, [])

    def test_a_human_separate_answer_blocks_and_flags_a_new_merge(self):
        models = FakeModels(
            ModelVerdict(True, 1.0),
            ModelVerdict(True, 1.0),
        )

        groups, report = plan_person_merges_resolved(
            self.people,
            {},
            decisions={frozenset(("A1", "A2")): DIFFERENT},
            models=models,
        )

        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "disputed")
        self.assertEqual(report[0]["rule"], "qwen_second_merge")
        self.assertEqual(len(models.first_calls), 1)
        self.assertEqual(len(models.second_calls), 1)

    def test_a_human_separate_answer_is_not_queued_again_when_models_agree(self):
        models = FakeModels(ModelVerdict(False, 1.0, "different people"))

        groups, report = plan_person_merges_resolved(
            self.people,
            {},
            decisions={frozenset(("A1", "A2")): DIFFERENT},
            models=models,
        )

        self.assertEqual((groups, report), ([], []))

    def test_a_transitive_component_cannot_bypass_a_human_separate_answer(self):
        people = [
            person("A1", "Nikolay Nikitin"),
            person("A2", "Nikolay O. Nikitin"),
            person("A3", "N. O. Nikitin"),
        ]

        groups, report = plan_person_merges_resolved(
            people,
            {},
            decisions={
                frozenset(("A1", "A2")): SAME,
                frozenset(("A2", "A3")): SAME,
                frozenset(("A1", "A3")): DIFFERENT,
            },
            models=FakeModels(),
        )

        self.assertEqual(groups, [])
        conflict = next(row for row in report if row.get("route") == "manual_conflict")
        self.assertEqual(conflict["status"], "held")
        self.assertEqual(conflict["persons"], ["A1", "A2", "A3"])
        self.assertEqual(conflict["manual_conflicts"], [["A1", "A3"]])


class DedupStageWiringTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Settings(
            data_dir=Path(temporary.name),
            person_resolution_enabled=True,
        )
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")
        self.prepared.write_models(
            "persons",
            [
                person("A1", "Nikolay O. Nikitin"),
                person("A2", "Nikolay Nikitin"),
            ],
        )

    @patch("pauk.pipeline.person_resolution.OpenRouterResolutionModels")
    def test_pipeline_uses_the_resolver_and_merges_two_positive_verdicts(
        self, model_factory
    ):
        model_factory.return_value = FakeModels(
            ModelVerdict(True, 0.9), ModelVerdict(True, 0.9)
        )

        result = DedupStage(self.prepared, self.raw, self.config).run()

        self.assertEqual(result["dedup_merged"], 1)
        self.assertEqual(review.questions(self.db), [])

    @patch("pauk.pipeline.person_resolution.OpenRouterResolutionModels")
    def test_pipeline_sends_a_rejected_pair_to_review_panel(
        self, model_factory
    ):
        model_factory.return_value = FakeModels(
            ModelVerdict(False, 0.9, "different people")
        )

        result = DedupStage(self.prepared, self.raw, self.config).run()

        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(result["dedup_candidates"], 1)
        (question,) = review.questions(self.db)
        self.assertEqual(question["kind"], review.PAIR)
        self.assertEqual(question["members"], ["A1", "A2"])
        self.assertEqual(question["evidence"]["route"], "qwen_first_separate")

    def test_conflicting_orcid_never_reaches_a_model_or_queue(self):
        people = [
            person("A1", "Ivan Petrov", orcid="0000-0001"),
            person("A2", "Ivan Petrov", orcid="0000-0002"),
        ]
        models = FakeModels(
            ModelVerdict(True, 1.0),
            ModelVerdict(True, 1.0),
        )

        groups, report = plan_person_merges_resolved(
            people,
            {"A1": "0000-0001", "A2": "0000-0002"},
            models=models,
        )

        self.assertEqual((groups, report), ([], []))
        self.assertEqual(models.first_calls, [])


class ResolverCacheTest(unittest.TestCase):
    """A stored verdict has to survive renumbering: merges upstream shift
    every pair number, and the verdict is about the two people, not about
    the number they were handed this run."""

    def setUp(self):
        self.config = Settings(openrouter_api_key="test-key")
        self.db = mongomock.MongoClient()["pauk_test"]
        self.models = OpenRouterResolutionModels(self.config, self.db, "sample")

    def evidence(self):
        return PairEvidence(
            person_a="A1", name_a="Ivan Petrov", person_b="A2", name_b="I. Petrov",
        )

    def answer(self, pair_id):
        return {"results": [{"id": pair_id, "duplicate": True, "confidence": 0.9,
                             "reason": "same person"}]}

    def test_a_verdict_stored_under_one_number_answers_another(self):
        calls = []

        def chat_json(self_client, prompt, **kwargs):
            calls.append(prompt)
            return {"results": [{"id": 7, "duplicate": True, "confidence": 0.9, "reason": "same"}]}

        with patch("pauk.sources.OpenRouterClient.chat_json", chat_json, create=True):
            first = self.models.first_many([(7, self.evidence())])
            second = self.models.first_many([(4210, self.evidence())])

        self.assertTrue(first[7].duplicate)
        self.assertTrue(second[4210].duplicate)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.db["llm_person_resolution_cache"].count_documents({}), 1)

    def test_the_key_still_separates_different_people(self):
        other = PairEvidence(person_a="B1", name_a="Anna Volkova",
                             person_b="B2", name_b="A. Volkova")
        keys = {
            self.models._fingerprint(request)
            for request in (
                _Request(1, "qwen_first", "sys", first_stage_payload(1, self.evidence()), 160, None),
                _Request(2, "qwen_first", "sys", first_stage_payload(2, other), 160, None),
            )
        }

        self.assertEqual(len(keys), 2)


class ResolverBatchingTest(unittest.TestCase):
    """The pool is fed in blocks, so every request must still be answered
    and counted exactly once across block boundaries."""

    def models(self, workers):
        config = Settings(openrouter_api_key="test-key", person_resolution_concurrency=workers)
        db = mongomock.MongoClient()["pauk_test"]
        return OpenRouterResolutionModels(config, db, "sample")

    def evidence(self, number):
        return PairEvidence(
            person_a=f"A{number}", name_a=f"Ivan Petrov{number}",
            person_b=f"B{number}", name_b=f"I. Petrov{number}",
        )

    def test_every_pair_is_answered_across_several_blocks(self):
        models = self.models(workers=2)
        items = [(number, self.evidence(number)) for number in range(30)]
        seen = []

        def invoke(request):
            seen.append(request.pair_id)
            return _Result(request, ModelVerdict(True, 0.9, "same"), None, None, None, None, False)

        with patch.object(OpenRouterResolutionModels, "_invoke", side_effect=invoke, autospec=False):
            verdicts = models.first_many(items)

        self.assertEqual(sorted(verdicts), [number for number, _ in items])
        self.assertEqual(sorted(seen), [number for number, _ in items])
        self.assertTrue(all(verdict.duplicate for verdict in verdicts.values()))


if __name__ == "__main__":
    unittest.main()
