import types
import unittest
from unittest.mock import patch

import mongomock

from pauk.pipeline import person_resolution_review as bridge


class ReviewBridgeTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db

    def test_missing_review_pr_is_a_safe_noop(self):
        missing = ModuleNotFoundError("missing", name="pauk.storage.review")
        with patch.object(bridge, "import_module", side_effect=missing):
            self.assertEqual(bridge.decisions(self.db), {})
            self.assertEqual(bridge.staff_choices(self.db), {})
            bridge.record(self.db, [{"status": "held"}], "stage")
            bridge.mark_applied(self.db, {})

    def test_held_pairs_use_the_review_panel_contract(self):
        calls = []
        backend = types.SimpleNamespace(
            decisions=lambda db, aliases: {frozenset(("A1", "A2")): "same"},
            staff_choices=lambda db, aliases: {},
            record_held=lambda db, report, source: calls.append(("held", report, source)),
            record_disputed=lambda db, report: calls.append(("disputed", report)),
            mark_applied_merges=lambda db, aliases: calls.append(("applied", aliases)),
        )
        with patch.object(bridge, "import_module", return_value=backend):
            answers = bridge.decisions(self.db, {"old": "A1"})
            report = [{"status": "held", "person_a": "A1", "person_b": "A2"}]
            bridge.record(self.db, report, "graph")
            bridge.mark_applied(self.db, {"A2": "A1"})

        self.assertEqual(answers, {frozenset(("A1", "A2")): "same"})
        self.assertEqual(calls[0], ("held", report, "graph"))
        self.assertEqual(calls[1], ("disputed", report))
        self.assertEqual(calls[2], ("applied", {"A2": "A1"}))


if __name__ == "__main__":
    unittest.main()
