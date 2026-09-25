import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph.person_resolution import ModelVerdict
from pauk.models import Person
from pauk.pipeline.person_resolution_planner import (
    DIFFERENT,
    SAME,
    plan_person_merges_resolved,
)
from pauk.pipeline.stages.dedup import (
    DedupStage,
    _finalize_person_merge_groups,
    _group_conflict,
)
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
        self.assertEqual(report[-1]["route"], "qwen_second_merge")
        self.assertEqual(report[-1]["rules"], ["qwen_second_merge"])
        self.assertEqual(len(models.first_calls), 1)
        self.assertEqual(len(models.second_calls), 1)

    def test_first_model_rejection_becomes_a_review_question(self):
        models = FakeModels(ModelVerdict(False, 0.8, "insufficient evidence"))

        groups, report = plan_person_merges_resolved(self.people, {}, models=models)

        self.assertEqual(groups, [])
        self.assertEqual(report[0]["status"], "held")
        self.assertEqual(report[0]["route"], "qwen_first_separate")
        self.assertEqual(report[0]["reason"], "insufficient evidence")
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

    def test_conflicted_component_keeps_exact_orcid_subgroups(self):
        people = [
            person("A1", "Sergey Makarov", orcid="0000-0001"),
            person("A2", "S. Makarov", orcid="0000-0001"),
            person("B", "Sergey Makarov"),
            person("C1", "Sergey Makarov", orcid="0000-0002"),
            person("C2", "S. Makarov", orcid="0000-0002"),
        ]

        groups, report = plan_person_merges_resolved(
            people,
            {
                "A1": "0000-0001",
                "A2": "0000-0001",
                "B": None,
                "C1": "0000-0002",
                "C2": "0000-0002",
            },
            decisions={
                frozenset(("A2", "B")): SAME,
                frozenset(("B", "C1")): SAME,
            },
            models=FakeModels(),
        )

        folded = [
            {group[0].id, *(duplicate.id for duplicate in group[1])}
            for group in groups
        ]
        self.assertCountEqual(folded, [{"A1", "A2"}, {"C1", "C2"}])
        self.assertEqual(
            {
                row["person_a"]: row["route"]
                for row in report
                if row["status"] == "merged"
            },
            {"A2": "same_orcid", "C2": "same_orcid"},
        )
        conflict = next(row for row in report if row.get("route") == "component_conflict")
        self.assertEqual(conflict["persons"], ["A1", "B", "C1"])
        self.assertEqual(
            conflict["held_because"], ["group spans 2 distinct ORCID values"]
        )


class ConflictedComponentPartitionTest(unittest.TestCase):
    def test_an_empty_identity_is_absent_rather_than_a_second_value(self):
        # An empty string used to count as an identity of its own while the
        # partitioner read the same field as absent. The record kept landing
        # back in the bucket it was supposed to leave, and the stage died of
        # a RecursionError instead of folding two obvious duplicates.
        people = [
            person("A1", "Sergey Makarov", orcid="0000-0001"),
            person("A2", "S. Makarov", orcid=""),
        ]
        people[0].email = "makarov@itmo.ru"
        people[1].email = ""
        pairs = [("A1", "A2")]

        groups, report = _finalize_person_merge_groups(
            pairs,
            {frozenset(pairs[0]): "qwen_second_merge"},
            {value.id: value for value in people},
            {value.id: value.orcid for value in people},
        )

        folded = [
            {group[0].id, *(duplicate.id for duplicate in group[1])}
            for group in groups
        ]
        self.assertEqual(folded, [{"A1", "A2"}])
        self.assertFalse([row for row in report if row["status"] == "held"])

    def test_identityless_records_follow_the_unique_nearest_identity(self):
        people = [
            person("A1", "Sergey Makarov", orcid="0000-0001"),
            person("A2", "S. Makarov", orcid="0000-0001"),
            person("X", "Sergey Makarov"),
            person("Y", "Sergey Makarov"),
            person("C1", "Sergey Makarov", orcid="0000-0002"),
            person("C2", "S. Makarov", orcid="0000-0002"),
        ]
        pairs = [
            ("A1", "A2"),
            ("A2", "X"),
            ("X", "Y"),
            ("Y", "C1"),
            ("C1", "C2"),
        ]

        groups, report = _finalize_person_merge_groups(
            pairs,
            {frozenset(pair): "qwen_second_merge" for pair in pairs},
            {value.id: value for value in people},
            {value.id: value.orcid for value in people},
        )

        folded = [
            {group[0].id, *(duplicate.id for duplicate in group[1])}
            for group in groups
        ]
        self.assertCountEqual(
            folded,
            [{"A1", "A2", "X"}, {"Y", "C1", "C2"}],
        )
        conflict = next(row for row in report if row.get("route") == "component_conflict")
        self.assertEqual(conflict["persons"], ["A1", "C1"])

    def test_equal_distance_identityless_record_stays_for_review(self):
        people = [
            person("A", "Sergey Makarov", orcid="0000-0001"),
            person("X", "Sergey Makarov"),
            person("C", "Sergey Makarov", orcid="0000-0002"),
        ]
        pairs = [("A", "X"), ("X", "C")]

        groups, report = _finalize_person_merge_groups(
            pairs,
            {frozenset(pair): "qwen_second_merge" for pair in pairs},
            {value.id: value for value in people},
            {value.id: value.orcid for value in people},
        )

        self.assertEqual(groups, [])
        conflict = next(row for row in report if row.get("route") == "component_conflict")
        self.assertEqual(conflict["persons"], ["A", "C", "X"])

    def test_partition_repeats_for_a_second_identity_conflict(self):
        first = person("A1", "Sergey Makarov", orcid="0000-0001")
        second = person("A2", "S. Makarov", orcid="0000-0001")
        other = person("B", "Sergey Makarov", orcid="0000-0001")
        first.email = "one@example.org"
        second.email = "one@example.org"
        other.email = "two@example.org"
        people = [first, second, other]
        pairs = [("A1", "A2"), ("A2", "B")]

        groups, report = _finalize_person_merge_groups(
            pairs,
            {frozenset(pair): "qwen_second_merge" for pair in pairs},
            {value.id: value for value in people},
            {value.id: value.orcid for value in people},
        )

        folded = [
            {group[0].id, *(duplicate.id for duplicate in group[1])}
            for group in groups
        ]
        self.assertEqual(folded, [{"A1", "A2"}])
        conflict = next(row for row in report if row.get("route") == "component_conflict")
        self.assertEqual(conflict["persons"], ["A1", "B"])

    def test_staff_subgroups_survive_a_staff_conflict(self):
        people = [
            person("A1", "One"),
            person("A2", "One alt"),
            person("B", "Bridge"),
            person("C1", "Two"),
            person("C2", "Two alt"),
        ]
        pairs = [("A1", "A2"), ("A2", "B"), ("B", "C1"), ("C1", "C2")]

        groups, report = _finalize_person_merge_groups(
            pairs,
            {frozenset(pair): "qwen_second_merge" for pair in pairs},
            {value.id: value for value in people},
            {},
            {"A1": "staff-1", "A2": "staff-1", "C1": "staff-2", "C2": "staff-2"},
        )

        folded = [
            {group[0].id, *(duplicate.id for duplicate in group[1])}
            for group in groups
        ]
        self.assertCountEqual(folded, [{"A1", "A2"}, {"C1", "C2"}])
        merged_routes = {
            row["person_a"]: row["route"]
            for row in report
            if row["status"] == "merged"
        }
        self.assertEqual(merged_routes, {"A2": "same_staff", "C2": "same_staff"})
        self.assertEqual(report[-1]["persons"], ["A1", "B", "C1"])
        self.assertEqual(report[-1]["route"], "component_conflict")

    def test_exact_identifier_does_not_override_another_profile_conflict(self):
        first = person("A1", "Sergey Makarov", orcid="0000-0001")
        second = person("A2", "S. Makarov", orcid="0000-0001")
        other = person("C", "Sergey Makarov", orcid="0000-0002")
        first.email = "first@example.org"
        second.email = "second@example.org"
        pairs = [("A1", "A2"), ("A2", "C")]

        groups, report = _finalize_person_merge_groups(
            pairs,
            {frozenset(pair): "manual" for pair in pairs},
            {value.id: value for value in (first, second, other)},
            {"A1": "0000-0001", "A2": "0000-0001", "C": "0000-0002"},
        )

        self.assertEqual(groups, [])
        self.assertEqual(report[-1]["persons"], ["A1", "A2", "C"])

    def test_partition_is_independent_of_pair_order(self):
        people = [
            person("A1", "One", orcid="0000-0001"),
            person("A2", "One alt", orcid="0000-0001"),
            person("B", "Bridge"),
            person("C1", "Two", orcid="0000-0002"),
            person("C2", "Two alt", orcid="0000-0002"),
        ]
        pairs = [("A1", "A2"), ("A2", "B"), ("B", "C1"), ("C1", "C2")]
        by_id = {value.id: value for value in people}
        orcids = {value.id: value.orcid for value in people}
        rules = {frozenset(pair): "qwen_second_merge" for pair in pairs}

        forward = _finalize_person_merge_groups(pairs, rules, by_id, orcids)
        reverse = _finalize_person_merge_groups(list(reversed(pairs)), rules, by_id, orcids)

        def normalized(result):
            groups, report = result
            folded = sorted(
                sorted([group[0].id, *(duplicate.id for duplicate in group[1])])
                for group in groups
            )
            held = sorted(
                row["persons"] for row in report if row["status"] == "held"
            )
            return folded, held

        self.assertEqual(normalized(forward), normalized(reverse))

    def test_randomized_partitions_never_emit_a_conflicting_group(self):
        randomizer = random.Random(205)
        with self.assertLogs("pauk.pipeline.stages.dedup", level="WARNING"):
            for iteration in range(250):
                people = [
                    person(
                        f"P{index}",
                        f"Person {index}",
                        orcid=(
                            f"orcid-{randomizer.randrange(5)}"
                            if randomizer.random() < 0.65
                            else None
                        ),
                    )
                    for index in range(randomizer.randrange(3, 30))
                ]
                for value in people:
                    if randomizer.random() < 0.35:
                        value.email = f"mail-{randomizer.randrange(4)}@example.org"
                    elif randomizer.random() < 0.15:
                        # An absent identity also arrives as an empty string.
                        value.email = ""
                        value.orcid = "" if randomizer.random() < 0.5 else value.orcid
                ids = [value.id for value in people]
                pairs = list(zip(ids, ids[1:], strict=False))
                pairs.extend(
                    tuple(randomizer.sample(ids, 2))
                    for _ in range(randomizer.randrange(len(ids)))
                )
                by_id = {value.id: value for value in people}
                orcids = {value.id: value.orcid for value in people}
                staff_ids = {
                    value.id: f"staff-{randomizer.randrange(5)}"
                    for value in people
                    if randomizer.random() < 0.45
                }

                groups, _report = _finalize_person_merge_groups(
                    pairs,
                    {frozenset(pair): "qwen_second_merge" for pair in pairs},
                    by_id,
                    orcids,
                    staff_ids,
                )

                emitted: set[str] = set()
                for canonical, duplicates in groups:
                    members = [
                        canonical.id,
                        *(duplicate.id for duplicate in duplicates),
                    ]
                    with self.subTest(iteration=iteration, members=members):
                        self.assertIsNone(
                            _group_conflict(members, by_id, orcids, staff_ids)
                        )
                        self.assertTrue(emitted.isdisjoint(members))
                    emitted.update(members)


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


if __name__ == "__main__":
    unittest.main()
