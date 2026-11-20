"""Pair selection shares recency rules without owning decision semantics."""

import unittest
from datetime import UTC, datetime, timedelta, timezone
from itertools import permutations

from pauk.pair_rows import latest_pair_rows


class LatestPairRowsTest(unittest.TestCase):
    def setUp(self):
        self.aliases = {"old": "current", "older": "current"}
        self.pair = frozenset(("first", "current"))
        self.moment = datetime(2026, 1, 1, tzinfo=UTC)

    def row(self, stable_id, member="current", **fields):
        return {"_id": stable_id, "members": ["first", member], **fields}

    def selected(self, rows, timestamp_field="when"):
        return latest_pair_rows(rows, self.aliases, timestamp_field=timestamp_field)

    def test_empty_input_and_independent_pairs(self):
        self.assertEqual(self.selected([]), {})
        first = self.row("first")
        other = {"_id": "other", "members": ["third", "fourth"]}
        self.assertEqual(self.selected([first, other]), {
            self.pair: first, frozenset(("third", "fourth")): other,
        })

    def test_newest_timestamp_wins_before_current_ids_or_stable_id(self):
        older = self.row("z", when=self.moment)
        newer = self.row("a", "old", when=self.moment + timedelta(seconds=1))
        for rows in permutations((older, newer)):
            self.assertIs(self.selected(rows)[self.pair], newer)

    def test_timestamp_parameter_and_legacy_normalization(self):
        legacy = self.row("z")
        explicit_null = self.row("y", "old", decided_at=None)
        naive = self.row("a", "older", decided_at=self.moment.replace(tzinfo=None))
        for rows in permutations((legacy, explicit_null, naive)):
            self.assertIs(self.selected(rows, timestamp_field="decided_at")[self.pair], naive)

    def test_current_ids_break_equal_missing_naive_and_aware_timestamps(self):
        for timestamp in (None, self.moment.replace(tzinfo=None), self.moment):
            with self.subTest(timestamp=timestamp):
                alias = self.row("z", "old", when=timestamp)
                current = self.row("a", when=timestamp)
                for rows in permutations((alias, current)):
                    self.assertIs(self.selected(rows)[self.pair], current)

    def test_aware_timestamps_compare_instants_not_local_clock(self):
        plus_two = timezone(timedelta(hours=2))
        older = self.row("z", when=datetime(2026, 1, 1, 2, tzinfo=plus_two))
        newer = self.row("a", "old", when=self.moment + timedelta(seconds=1))
        self.assertIs(self.selected([older, newer])[self.pair], newer)
        same_instant = self.row("a", "old", when=self.moment.astimezone(plus_two))
        self.assertIs(self.selected([same_instant, older])[self.pair], older)

    def test_stable_id_breaks_ties_independently_of_input_order(self):
        first = self.row("a", "old")
        second = self.row("z", "older")
        for rows in permutations((first, second)):
            self.assertIs(self.selected(rows)[self.pair], second)

    def test_returns_original_row_and_members_without_mutation_or_projection(self):
        row = self.row("source", "old", when=self.moment.replace(tzinfo=None), verdict=None,
                       skipped_at=self.moment, provenance={"actor": "human"})
        members = row["members"]
        self.assertIs(self.selected(iter([row]))[self.pair], row)
        self.assertIs(row["members"], members)
        self.assertEqual(members, ["first", "old"])
        self.assertIsNone(row["when"].tzinfo)
        self.assertEqual(row["provenance"], {"actor": "human"})

    def test_collapsed_pair_is_excluded_and_unordered_members_share_a_key(self):
        collapsed = {"_id": "collapsed", "members": ["current", "old"]}
        first = self.row("a")
        reverse = {"_id": "z", "members": ["current", "first"]}
        self.assertEqual(self.selected([collapsed, first, reverse]), {self.pair: reverse})

    def test_aliases_are_optional_and_do_not_filter_decision_fields(self):
        row = self.row("row", "old", verdict=None, skipped_at=self.moment)
        self.assertEqual(latest_pair_rows([row], timestamp_field="when"), {
            frozenset(("first", "old")): row,
        })
