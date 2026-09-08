import unittest

import mongomock

from pauk.storage import review


def held_pair(a="A1", b="A2", because="identical name with nothing corroborating it"):
    """One held row in the shape plan_person_merges actually produces."""
    return {"status": "held",
            "person_a": a, "name_a": "A. V. Yulin",
            "person_b": b, "name_b": "A. V. Yulin",
            "shared_coauthors": 0, "shared_departments": 0,
            "shared_fields": ["Physics and Astronomy"],
            "held_because": [because]}


def held_group(members=("A1", "A2", "A3"), because="group spans 2 distinct ORCID values"):
    """A group the rules refused whole. A different shape, same report."""
    return {"status": "held", "persons": sorted(members),
            "names": ["Andrey Bogdanov"] * len(members),
            "held_because": [because]}


class QuestionIdTest(unittest.TestCase):
    def test_the_same_pair_either_way_round_is_one_question(self):
        # The queue is fed by a run that has no reason to order a pair the
        # way the last one did; mirrored duplicates would each need
        # answering, and answering one would not settle the other.
        self.assertEqual(review.question_id(review.PAIR, ["A2", "A1"]),
                         review.question_id(review.PAIR, ["A1", "A2"]))

    def test_a_repeated_member_does_not_make_a_question(self):
        with self.assertRaises(review.ReviewError):
            review.question_id(review.PAIR, ["A1", "A1"])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(review.ReviewError):
            review.question_id("person_maybe", ["A1", "A2"])

    def test_a_group_keeps_every_member_in_its_key(self):
        key = review.question_id(review.GROUP, ["A3", "A1", "A2"])
        self.assertEqual(key, "person_group:A1:A2:A3")


class RecordHeldTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_only_held_rows_become_questions(self):
        review.record_held(self.db, [held_pair(), {"status": "merged", "person_a": "A9",
                                                   "person_b": "A8"}])
        self.assertEqual(review.count(self.db), 1)

    def test_a_pair_held_twice_is_one_document(self):
        # The whole reason the key is derived rather than generated: every
        # run re-examines the same pairs and refuses them again.
        review.record_held(self.db, [held_pair()])
        review.record_held(self.db, [held_pair()])
        self.assertEqual(review.count(self.db), 1)

    def test_both_shapes_of_held_row_are_stored(self):
        review.record_held(self.db, [held_pair(), held_group()])
        self.assertEqual(review.count(self.db, kind=review.PAIR), 1)
        self.assertEqual(review.count(self.db, kind=review.GROUP), 1)

    def test_the_evidence_is_kept_for_the_page_to_show(self):
        review.record_held(self.db, [held_pair()])
        (row,) = review.questions(self.db)
        self.assertEqual(row["evidence"]["shared_coauthors"], 0)
        # Names come out in the order of `members`, not of the report.
        self.assertEqual(row["evidence"]["names"], ["A. V. Yulin", "A. V. Yulin"])

    def test_a_later_run_refreshes_the_evidence(self):
        # A pair can gain a shared coauthor between runs. The conflict
        # screen compares what was known then with what is known now, so
        # the newer evidence has to land.
        review.record_held(self.db, [held_pair()])
        newer = held_pair()
        newer["shared_coauthors"] = 3
        review.record_held(self.db, [newer])
        (row,) = review.questions(self.db)
        self.assertEqual(row["evidence"]["shared_coauthors"], 3)

    def test_a_later_run_does_not_wipe_the_answer(self):
        # The run that asks again knows nothing about the person who
        # answered. Overwriting the document wholesale would lose them.
        review.record_held(self.db, [held_pair()])
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT,
                              actor="user:roman", note="two different physicists")
        review.record_held(self.db, [held_pair()])
        (row,) = review.questions(self.db)
        self.assertEqual(row["verdict"], review.DIFFERENT)
        self.assertEqual(row["note"], "two different physicists")

    def test_nothing_held_writes_nothing(self):
        self.assertEqual(review.record_held(self.db, []), 0)
        self.assertEqual(review.count(self.db), 0)


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        review.record_held(self.db, [held_pair(), held_group()])

    def test_answering_records_who_and_why(self):
        row = review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.SAME,
                                    actor="user:roman", note="same lab")
        self.assertEqual(row["verdict"], review.SAME)
        self.assertEqual(row["actor"], "user:roman")
        self.assertIsNotNone(row["decided_at"])

    def test_an_unknown_verdict_is_refused(self):
        with self.assertRaises(review.ReviewError):
            review.record_verdict(self.db, review.PAIR, ["A1", "A2"], "probably")

    def test_a_refused_group_cannot_be_called_one_person(self):
        # It was refused because its members disagree about an identity
        # field. "All one person" would mean ignoring two ORCIDs, which the
        # merge itself refuses anyway.
        with self.assertRaises(review.ReviewError):
            review.record_verdict(self.db, review.GROUP, ["A1", "A2", "A3"], review.SAME)

    def test_a_group_can_be_left_apart(self):
        row = review.record_verdict(self.db, review.GROUP, ["A1", "A2", "A3"],
                                    review.DIFFERENT, actor="user:roman")
        self.assertEqual(row["verdict"], review.DIFFERENT)

    def test_answering_a_question_nobody_asked_still_holds(self):
        # A person may answer from the CLI about a pair the current data no
        # longer produces. The answer has to hold when it does again.
        review.record_verdict(self.db, review.PAIR, ["B1", "B2"], review.DIFFERENT)
        self.assertEqual(review.decisions(self.db)[frozenset({"B1", "B2"})],
                         review.DIFFERENT)

    def test_withdrawing_leaves_the_question_in_the_queue(self):
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.SAME)
        self.assertTrue(review.withdraw(self.db, review.PAIR, ["A1", "A2"]))
        self.assertEqual(review.count(self.db, answered=True), 0)
        self.assertEqual(review.count(self.db, answered=False), 2)

    def test_applying_is_recorded_apart_from_deciding(self):
        # The two happen at different times: a pair answered before its
        # group is published has nothing to merge yet.
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.SAME)
        (row,) = review.questions(self.db, kind=review.PAIR)
        self.assertNotIn("applied_at", row)
        self.assertTrue(review.mark_applied(self.db, review.PAIR, ["A1", "A2"]))
        (row,) = review.questions(self.db, kind=review.PAIR)
        self.assertIsNotNone(row["applied_at"])

    def test_only_a_merge_can_be_marked_applied(self):
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT)
        self.assertFalse(review.mark_applied(self.db, review.PAIR, ["A1", "A2"]))


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        review.record_held(self.db, [held_pair()])

    def test_unanswered_questions_are_not_decisions(self):
        self.assertEqual(review.decisions(self.db), {})

    def test_an_answer_is_keyed_by_the_people_it_is_about(self):
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT)
        self.assertEqual(review.decisions(self.db),
                         {frozenset({"A1", "A2"}): review.DIFFERENT})

    def test_a_folded_id_still_finds_its_answer(self):
        # A person merged into another keeps the answers made about them
        # under an id that no longer exists. Without the alias map they
        # would quietly stop applying.
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT)
        found = review.decisions(self.db, aliases={"A2": "A7"})
        self.assertEqual(found, {frozenset({"A1", "A7"}): review.DIFFERENT})

    def test_a_pair_folded_into_one_person_stops_being_a_question(self):
        # Both sides are the same node now. Whatever was decided, there is
        # nothing left to decide.
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT)
        self.assertEqual(review.decisions(self.db, aliases={"A2": "A1"}), {})


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        review.record_held(self.db, [
            held_pair("A1", "A2"),
            held_pair("A3", "A4", because="only one person is ITMO-affiliated"),
            held_group(),
        ])

    def test_the_queue_can_be_narrowed_to_what_is_still_open(self):
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT)
        self.assertEqual(len(review.questions(self.db, answered=False)), 2)
        self.assertEqual(len(review.questions(self.db, answered=True)), 1)

    def test_the_queue_can_be_narrowed_by_reason(self):
        # 104 of one reason and 18 of another came out of one real run, and
        # the valuable ones are the small pile.
        found = review.questions(
            self.db, reason="identical name with nothing corroborating it")
        self.assertEqual([row["members"] for row in found], [["A1", "A2"]])

    def test_the_reasons_offered_are_the_ones_actually_there(self):
        self.assertEqual(review.reasons(self.db), [
            "group spans 2 distinct ORCID values",
            "identical name with nothing corroborating it",
            "only one person is ITMO-affiliated",
        ])

    def test_paging_neither_repeats_nor_skips(self):
        first = review.questions(self.db, limit=2)
        second = review.questions(self.db, limit=2, skip=2)
        self.assertEqual(len(first) + len(second), 3)
        self.assertFalse({row["_id"] for row in first} & {row["_id"] for row in second})


if __name__ == "__main__":
    unittest.main()


class PressingTest(unittest.TestCase):
    """The default view. 278 questions in one run, and most of them are
    piles where the refusal is right and a reviewer would only be reading."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        review.record_held(self.db, [
            held_pair("A1", "A2"),
            held_pair("A3", "A4", because="only one person is ITMO-affiliated"),
            held_pair("A5", "A6", because="name is given as initials"),
            held_group(),
        ])

    def test_the_default_view_is_the_short_pile(self):
        found = review.questions(self.db, pressing=True)
        self.assertEqual(sorted(row["_id"] for row in found),
                         ["person_group:A1:A2:A3", "person_pair:A1:A2"])

    def test_a_refused_group_is_always_pressing(self):
        # Its wording changes with the field that split it, so it cannot be
        # picked out by reason.
        review.record_held(self.db, [held_group(("B1", "B2"),
                                                because="group spans 3 distinct email values")])
        self.assertEqual(review.count(self.db, pressing=True, kind=review.GROUP), 2)

    def test_the_counter_and_the_page_agree(self):
        self.assertEqual(review.count(self.db, pressing=True),
                         len(review.questions(self.db, pressing=True)))

    def test_nothing_is_hidden_from_the_full_queue(self):
        self.assertEqual(review.count(self.db), 4)


class SkipTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        review.record_held(self.db, [held_pair("A1", "A2"), held_pair("A3", "A4")])

    def test_skipping_separates_read_from_unread(self):
        review.skip(self.db, review.PAIR, ["A1", "A2"], actor="user:roman")
        self.assertEqual(review.count(self.db, skipped=True), 1)
        self.assertEqual(review.count(self.db, skipped=False), 1)

    def test_a_skip_is_not_a_decision(self):
        # The rules must never see it: nobody decided anything.
        review.skip(self.db, review.PAIR, ["A1", "A2"])
        self.assertEqual(review.decisions(self.db), {})

    def test_answering_later_clears_the_skip(self):
        review.skip(self.db, review.PAIR, ["A1", "A2"])
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT)
        self.assertEqual(review.count(self.db, skipped=True), 0)

    def test_skipping_a_question_nobody_asked_does_nothing(self):
        self.assertFalse(review.skip(self.db, review.PAIR, ["Z1", "Z2"]))


class NameAlignmentTest(unittest.TestCase):
    """Names have to follow the ids, not the order the report was written.

    Members are sorted; a pair is reported in whatever order the blocking
    emitted it. Lining the two up wrongly puts one person's name against
    the other's id, and nothing on the page would show it.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_a_pair_reported_the_other_way_round_keeps_its_names(self):
        row = held_pair()
        row["person_a"], row["name_a"] = "A9", "Zinaida Orlova"
        row["person_b"], row["name_b"] = "A1", "Ivan Smirnov"
        review.record_held(self.db, [row])
        (stored,) = review.questions(self.db)
        self.assertEqual(stored["members"], ["A1", "A9"])
        self.assertEqual(stored["evidence"]["names"], ["Ivan Smirnov", "Zinaida Orlova"])

    def test_a_group_keeps_its_names_against_its_members(self):
        row = held_group(("B2", "B1"))
        row["persons"], row["names"] = ["B2", "B1"], ["Second", "First"]
        review.record_held(self.db, [row])
        (stored,) = review.questions(self.db)
        self.assertEqual(stored["members"], ["B1", "B2"])
        self.assertEqual(stored["evidence"]["names"], ["First", "Second"])


class SplitGroupTest(unittest.TestCase):
    """A refused group is answered by naming who inside it is one person.

    It cannot be answered as a whole: it was refused precisely because its
    members disagree about an identity field. The split is written as
    ordinary pair answers, because that is what the rules read.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        review.record_held(self.db, [held_group(("A1", "A2", "A3"))])

    def test_the_chosen_records_become_one_person(self):
        review.record_split(self.db, ["A1", "A2", "A3"], ["A1", "A2"],
                            actor="user:roman")
        self.assertEqual(review.decisions(self.db)[frozenset({"A1", "A2"})], review.SAME)

    def test_the_rest_are_told_apart_from_them(self):
        # Without this the rules rebuild the same group through the members
        # left over and refuse it again for the same reason.
        review.record_split(self.db, ["A1", "A2", "A3"], ["A1", "A2"])
        found = review.decisions(self.db)
        self.assertEqual(found[frozenset({"A1", "A3"})], review.DIFFERENT)
        self.assertEqual(found[frozenset({"A2", "A3"})], review.DIFFERENT)

    def test_the_group_question_is_closed(self):
        # "Not all of you are one person" is now plainly true of it, and an
        # answered question stops coming back.
        review.record_split(self.db, ["A1", "A2", "A3"], ["A1", "A2"])
        (group,) = review.questions(self.db, kind=review.GROUP)
        self.assertEqual(group["verdict"], review.DIFFERENT)

    def test_who_split_it_is_recorded_on_every_answer(self):
        review.record_split(self.db, ["A1", "A2", "A3"], ["A1", "A2"],
                            actor="user:roman", note="разные кафедры")
        for row in review.questions(self.db, answered=True):
            self.assertEqual(row["actor"], "user:roman")
            self.assertEqual(row["note"], "разные кафедры")

    def test_calling_the_whole_group_one_person_is_refused(self):
        with self.assertRaises(review.ReviewError):
            review.record_split(self.db, ["A1", "A2", "A3"], ["A1", "A2", "A3"])

    def test_one_record_is_not_a_split(self):
        with self.assertRaises(review.ReviewError):
            review.record_split(self.db, ["A1", "A2", "A3"], ["A1"])

    def test_somebody_outside_the_group_is_refused(self):
        with self.assertRaises(review.ReviewError):
            review.record_split(self.db, ["A1", "A2", "A3"], ["A1", "B9"])

    def test_a_bigger_group_splits_the_same_way(self):
        review.record_held(self.db, [held_group(("B1", "B2", "B3", "B4", "B5"))])
        written = review.record_split(self.db, ["B1", "B2", "B3", "B4", "B5"],
                                      ["B1", "B2", "B3"])
        # Three pairs inside the subset, six across the split.
        self.assertEqual(written, 9)


class SeparatorsInIdsTest(unittest.TestCase):
    """An id that carries the characters the key is built from.

    The key joins on ":" and the form that answers it joins on ",". Person
    ids cannot hold either today, but a LinkCandidate id turned out to be a
    URL once already, and the failure there was silent.
    """

    def test_a_colon_in_an_id_is_refused(self):
        with self.assertRaises(review.ReviewError):
            review.question_id(review.PAIR, ["A1", "https://x/y"])

    def test_a_comma_in_an_id_is_refused(self):
        with self.assertRaises(review.ReviewError):
            review.question_id(review.PAIR, ["A1", "Smith, John"])

    def test_the_ids_the_pipeline_makes_are_accepted(self):
        # OpenAlex ids, and the two local forms _fallback_person_id builds.
        for member in ("A5012742131", "orcid_0000-0002-1825-0097", "name_9f86d081884c"):
            with self.subTest(member=member):
                review.question_id(review.PAIR, ["A1", member])
