import unittest

import mongomock

from pauk.pipeline.progress_record import COLLECTION, ProgressRecord


class ProgressRecordTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db

    def row(self):
        return self.db[COLLECTION].find_one({"_id": "g1"})

    def test_stage_then_counts_then_next_stage(self):
        with ProgressRecord(self.db, "run", "g1") as progress:
            progress("persons", 0, 10)
            self.assertEqual(self.row()["stage"], "persons")
            self.assertIsNone(self.row()["rows_done"])

            progress("Authors: OpenAlex and ORCID 120/3000", 0, 10)
            row = self.row()
            self.assertEqual((row["stage"], row["label"], row["rows_done"], row["rows_total"]),
                             ("persons", "Authors: OpenAlex and ORCID", 120, 3000))

            progress("departments", 1, 10)
            row = self.row()
            self.assertEqual((row["stage"], row["stage_index"], row["rows_done"]), ("departments", 1, None))
            self.assertIsNone(row["finished_at"])
        self.assertEqual(self.row()["outcome"], "done")
        self.assertIsNotNone(self.row()["finished_at"])

    def test_failure_is_recorded_and_raised(self):
        with self.assertRaises(RuntimeError), ProgressRecord(self.db, "run", "g1"):
            raise RuntimeError("boom")
        self.assertEqual(self.row()["outcome"], "failed")

    def test_a_write_failure_does_not_stop_the_run(self):
        from pymongo.errors import PyMongoError

        class Broken:
            def update_one(self, *args, **kwargs):
                raise PyMongoError("down")

        with ProgressRecord({COLLECTION: Broken()}, "run", "g1") as progress:
            progress("persons", 0, 10)


if __name__ == "__main__":
    unittest.main()
