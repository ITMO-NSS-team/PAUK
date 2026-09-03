"""The snapshot and the stand that loads it must name the same collections.

Two lists in two languages, and the divergence only shows up as a rehearsal
that came up a collection short — with every author it meets created fresh
instead of merged with, which is the one thing the rehearsal exists to test.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location("snapshot_mongo", SCRIPTS / "snapshot_mongo.py")
snapshot_mongo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = snapshot_mongo
_spec.loader.exec_module(snapshot_mongo)

REHEARSAL_UP = (SCRIPTS / "rehearsal_up.sh").read_text(encoding="utf-8")


def fallback_collections() -> list[str]:
    """The list rehearsal_up.sh falls back to for a manifest-less snapshot."""
    match = re.search(r"COLLECTIONS=\((.*?)\)", REHEARSAL_UP, re.S)
    assert match, "rehearsal_up.sh no longer holds a COLLECTIONS=( ... ) array"
    return match.group(1).split()


class CollectionListsAgreeTest(unittest.TestCase):
    def test_the_stand_fallback_names_what_the_snapshot_writes(self):
        self.assertEqual(sorted(fallback_collections()), sorted(snapshot_mongo.DEFAULT))

    def test_the_snapshot_covers_the_collections_the_pipeline_merges_into(self):
        # Publications, departments and organizations are read-only to the
        # harvest and were left out of the defaults for that reason; the stand
        # imports them because a pipeline run needs them to merge against.
        self.assertLessEqual(
            {"publications", "persons", "departments", "organizations",
             "repositories", "repo_links", "github_profiles"},
            set(snapshot_mongo.DEFAULT))

    def test_the_stand_reads_the_manifest_the_snapshot_writes(self):
        # The fallback is for snapshots taken before the manifest existed;
        # a fresh one carries its own list, so the two cannot drift.
        self.assertIn("manifest.json", REHEARSAL_UP)


if __name__ == "__main__":
    unittest.main()
