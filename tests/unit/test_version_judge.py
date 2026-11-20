"""Successful version verdicts keep their recency after publication folds."""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import mongomock

from pauk.graph.version_judge import VERDICTS_COLLECTION, VersionJudge, stored_verdicts


class VerdictRecencyTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().pauk_test
        self.collection = self.db[VERDICTS_COLLECTION]
        self.aliases = {"W9": "W2", "W8": "W2"}
        self.pair = frozenset(("W1", "W2"))
        self.moment = datetime(2026, 1, 1, tzinfo=UTC)

    def save(self, other, verdict, judged_at=None):
        row = {"_id": f"W1:{other}", "members": ["W1", other], "same_work": verdict}
        if judged_at is not None:
            row["judged_at"] = judged_at
        self.collection.insert_one(row)

    def test_newest_success_wins_over_alias_id_order(self):
        for newest in (True, False):
            with self.subTest(newest=newest):
                self.collection.delete_many({})
                self.save("W9", not newest, self.moment)
                self.save("W2", newest, self.moment + timedelta(seconds=1))
                self.assertEqual(stored_verdicts(self.db, self.aliases)[self.pair]["same_work"], newest)

    def test_newer_alias_verdict_wins_over_older_canonical_verdict(self):
        self.save("W2", True, self.moment)
        self.save("W9", False, self.moment + timedelta(seconds=1))
        self.assertFalse(stored_verdicts(self.db, self.aliases)[self.pair]["same_work"])

    def test_timestamp_ties_and_legacy_rows_prefer_current_ids(self):
        for moment in (None, self.moment):
            with self.subTest(moment=moment):
                self.collection.delete_many({})
                self.save("W9", False, moment)
                self.save("W2", True, moment)
                self.assertTrue(stored_verdicts(self.db, self.aliases)[self.pair]["same_work"])

    def test_legacy_alias_collision_has_stable_tie_breaker(self):
        self.save("W9", False)
        self.save("W8", True)
        self.assertFalse(stored_verdicts(self.db, self.aliases)[self.pair]["same_work"])

    def test_timestamped_success_wins_over_legacy_alias(self):
        self.save("W9", False)
        self.save("W2", True, self.moment)
        self.assertTrue(stored_verdicts(self.db, self.aliases)[self.pair]["same_work"])

    def test_scoped_lookup_keeps_recency_and_excludes_unrelated_pairs(self):
        self.save("W9", False, self.moment)
        self.save("W2", True, self.moment + timedelta(seconds=1))
        self.collection.insert_one({"_id": "W3:W4", "members": ["W3", "W4"], "same_work": False})
        result = stored_verdicts(self.db, self.aliases, ({"W1"}, {"W2", "W9"}))
        self.assertEqual(result, {self.pair: {"same_work": True, "reason": None}})

    def test_mixed_naive_and_aware_timestamps_preserve_newest_success_and_projection(self):
        original = {"_id": "W1:W9", "members": ["W1", "W9"], "same_work": False,
                    "reason": "The newest successful routing", "judged_at": self.moment + timedelta(seconds=1),
                    "model": "test-model"}
        current = {"_id": "W1:W2", "members": ["W1", "W2"], "same_work": True,
                   "judged_at": self.moment.replace(tzinfo=None)}
        collection = Mock()
        collection.find.return_value = iter((original, current))
        result = stored_verdicts({VERDICTS_COLLECTION: collection}, self.aliases)
        self.assertEqual(result, {self.pair: {"same_work": False, "reason": "The newest successful routing"}})
        self.assertEqual(original["members"], ["W1", "W9"])
        self.assertEqual(original["judged_at"], self.moment + timedelta(seconds=1))
        self.assertIsNone(current["judged_at"].tzinfo)
        collection.find.assert_called_once_with({})

    def test_collapsed_self_pair_is_not_a_verdict(self):
        self.collection.insert_one({"_id": "W2:W9", "members": ["W2", "W9"], "same_work": True})
        self.assertEqual(stored_verdicts(self.db, self.aliases), {})

    def client(self, result):
        return SimpleNamespace(chat_json=Mock(return_value=result), last_error="offline", model="test-model")

    def test_successful_force_survives_reload_with_an_old_alias_key(self):
        self.save("W9", False)
        judge = VersionJudge(
            known=stored_verdicts(self.db, self.aliases),
            client=self.client({"same_work": True, "reason": "The same work"}),
            force=True,
            store=self.db,
        )
        before = datetime.now(UTC) - timedelta(seconds=1)
        verdict = judge({"id": "W1"}, {"id": "W2"})
        self.assertTrue(verdict["same_work"])
        persisted = self.collection.find_one({"_id": "W1:W2"})
        self.assertGreaterEqual(persisted["judged_at"].replace(tzinfo=UTC), before)
        reloaded = VersionJudge(known=stored_verdicts(self.db, self.aliases))
        self.assertEqual(reloaded({"id": "W1"}, {"id": "W2"}), verdict)
        self.assertEqual(judge.asked, 1)

    def test_failed_force_preserves_success_and_its_timestamp(self):
        self.save("W2", True, self.moment)
        original = self.collection.find_one({"_id": "W1:W2"})
        for result in (None, {"same_work": "true"}):
            with self.subTest(result=result):
                judge = VersionJudge(
                    known=stored_verdicts(self.db), client=self.client(result), force=True, store=self.db
                )
                self.assertTrue(judge({"id": "W1"}, {"id": "W2"})["same_work"])
                self.assertEqual(judge.failed, 1)
                self.assertEqual(self.collection.find_one({"_id": "W1:W2"}), original)
