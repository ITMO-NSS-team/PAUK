"""Which enrichment stage a run is on.

Ten stages, hours end to end. Without this the only thing a run says about
itself is a heartbeat, and "жива" answers a different question from "где".
"""

import unittest
from unittest.mock import patch

import mongomock

from pauk.pipeline import enrich
from pauk.pipeline.enrich import Enricher
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


def stage(stage_name: str):
    """A stage that does nothing but answer to its name."""
    return type(f"Fake{stage_name}", (), {
        "name": stage_name,
        "__init__": lambda self, *args, **kwargs: None,
        "run": lambda self: {stage_name: 1},
    })


class OnStageTest(unittest.TestCase):
    def setUp(self):
        db = mongomock.MongoClient()["pauk_test"]
        self.enricher = Enricher(PreparedStore(db, "sample"), RawStore(db, "sample"),
                                 Settings())
        self.stages = (stage("pdf"), stage("persons"), stage("dedup"))
        self.seen = []

    def run_all(self, on_stage=None):
        with patch.object(enrich, "ALL_STAGES", self.stages):
            return self.enricher.run(on_stage=on_stage)

    def test_every_stage_announces_itself_before_it_runs(self):
        self.run_all(lambda name, done, total: self.seen.append(name))
        self.assertEqual(self.seen, ["pdf", "persons", "dedup"])

    def test_the_count_says_how_far_along_the_run_is(self):
        self.run_all(lambda name, done, total: self.seen.append((done, total)))
        self.assertEqual(self.seen, [(0, 3), (1, 3), (2, 3)])

    def test_a_run_nobody_is_watching_still_works(self):
        # The hook is optional: the CLI passes nothing.
        self.assertEqual(self.run_all(), {"pdf": 1, "persons": 1, "dedup": 1})

    def test_one_named_stage_counts_only_itself(self):
        seen = []
        with patch.object(enrich, "ALL_STAGES", self.stages):
            self.enricher.stages = {s.name: s for s in self.stages}
            self.enricher.run("dedup", on_stage=lambda name, done, total: seen.append(
                (name, done, total)))
        self.assertEqual(seen, [("dedup", 0, 1)])


if __name__ == "__main__":
    unittest.main()
