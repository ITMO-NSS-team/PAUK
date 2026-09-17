import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mongomock

from pauk.graph.person_resolution import ModelVerdict
from pauk.models import Person
from pauk.pipeline.person_resolution_planner import (
    DIFFERENT,
    SAME,
    plan_person_merges_resolved,
)
from pauk.pipeline.stages.dedup import DedupStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


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
        groups, report = plan_person_merges_resolved(self.people, {}, models=FakeModels())

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

    @staticmethod
    def review_backend():
        return SimpleNamespace(
            decisions=Mock(return_value={}),
            record_held=Mock(),
            record_disputed=Mock(),
            mark_applied_merges=Mock(),
        )

    @patch("pauk.pipeline.person_resolution.OpenRouterResolutionModels")
    @patch("pauk.pipeline.person_resolution_review._backend")
    def test_pipeline_uses_the_resolver_and_merges_two_positive_verdicts(self, backend_factory, model_factory):
        backend = self.review_backend()
        backend_factory.return_value = backend
        model_factory.return_value = FakeModels(ModelVerdict(True, 0.9), ModelVerdict(True, 0.9))

        result = DedupStage(self.prepared, self.raw, self.config).run()

        self.assertEqual(result["dedup_merged"], 1)
        backend.record_held.assert_called_once()
        backend.mark_applied_merges.assert_called_once()

    @patch("pauk.pipeline.person_resolution.OpenRouterResolutionModels")
    @patch("pauk.pipeline.person_resolution_review._backend")
    def test_pipeline_sends_a_rejected_pair_to_pr177_review(self, backend_factory, model_factory):
        backend = self.review_backend()
        backend_factory.return_value = backend
        model_factory.return_value = FakeModels(ModelVerdict(False, 0.9, "different people"))

        result = DedupStage(self.prepared, self.raw, self.config).run()

        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(result["dedup_candidates"], 1)
        report = backend.record_held.call_args.args[1]
        self.assertEqual(report[0]["status"], "held")
        self.assertEqual({report[0]["person_a"], report[0]["person_b"]}, {"A1", "A2"})

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


if __name__ == "__main__":
    unittest.main()
