"""Reassessment plans preserve source data and never mutate the database."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import mongomock
from bson import ObjectId, json_util

from pauk.models import Person
from pauk.pipeline.membership import ITMO_ROR_ID, POLICY_VERSION, apply_membership
from scripts.reassess_membership import COLLECTIONS, _preserve_source_metadata, write_plan


class MembershipReassessmentPlanTest(unittest.TestCase):
    def test_duplicate_source_rows_keep_distinct_metadata(self):
        original = [{"publication_id": "W1", "position": 1, "extra": marker} for marker in ("first", "second")]
        updated = [{"publication_id": "W1", "position": 1}] * 2
        self.assertEqual(_preserve_source_metadata(original, updated, ("publication_id", "position")), original)

    def setUp(self):
        self.db = mongomock.MongoClient().membership_plan
        self.original = {
            "_id": ObjectId(), "id": "A1", "is_itmo": True,
            "name_raw": "A. Example", "groups": ["smoke", "full"],
            "_version": 7, "extra_source_field": {"keep": [1, None]},
            "emails": ["a@example.test"], "authored": [],
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        self.db.persons.insert_one(self.original)

    def snapshot(self):
        return {name: list(self.db[name].find({})) for name in COLLECTIONS}

    def test_preserves_unrelated_fields_and_every_database_collection(self):
        self.db.raw.insert_one({"source": "unknown", "payload": {"keep": True}})
        self.db.publications.insert_one({"id": "W1", "groups": ["other"]})
        self.db.person_merge_sources.insert_one({"person_id": "old", "snapshot": {"x": 1}})
        before = self.snapshot()
        rebuilt = Person.model_validate(self.original)
        rebuilt.membership_status = "unsupported"
        rebuilt.membership_policy = POLICY_VERSION
        rebuilt.is_itmo = False
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "plan"
            with patch("scripts.reassess_membership.rebuild_from_global_sources", return_value=rebuilt):
                summary = write_plan(self.db, out)
            output = json_util.loads((out / "persons.jsonl").read_text(encoding="utf-8"))
            for key, value in before["persons"][0].items():
                if key != "is_itmo":
                    self.assertEqual(output[key], value, key)
            self.assertFalse(output["is_itmo"])
            self.assertEqual(summary["cleared"], 1)
        self.assertEqual(self.snapshot(), before)

    def test_existing_output_is_never_overwritten(self):
        before = self.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            marker = out / "persons.jsonl"
            marker.write_text("original", encoding="utf-8")
            with patch("scripts.reassess_membership.rebuild_from_global_sources") as rebuild:
                with self.assertRaises(FileExistsError):
                    write_plan(self.db, out)
                rebuild.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.snapshot(), before)

    def test_unrelated_nested_source_metadata_is_preserved(self):
        authored = {
            "publication_id": "W1", "position": 2, "is_corresponding": True,
            "affiliation": "Original text", "provider_metadata": {"source_row": "keep"},
        }
        affiliation = {
            "name": "Other university", "source": "openalex", "years": [2024],
            "provider_metadata": {"original": True},
        }
        self.db.persons.update_one({"id": "A1"}, {"$set": {
            "authored": [authored], "affiliations": [affiliation],
        }})
        rebuilt = Person.model_validate(self.db.persons.find_one({"id": "A1"}))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "plan"
            with patch("scripts.reassess_membership.rebuild_from_global_sources", return_value=rebuilt):
                write_plan(self.db, out)
            output = json_util.loads((out / "persons.jsonl").read_text(encoding="utf-8"))
            for key, value in authored.items():
                self.assertEqual(output["authored"][0].get(key), value, key)
            for key, value in affiliation.items():
                self.assertEqual(output["affiliations"][0].get(key), value, key)

    def test_plan_remains_negative_after_corrected_orcid_is_replayed(self):
        identifier = "0000-0002-1825-0097"
        affiliation = {
            "name": "ITMO", "source": "orcid", "ror": ITMO_ROR_ID,
            "source_person_id": "A1", "source_id": identifier,
        }
        self.db.persons.update_one({"id": "A1"}, {"$set": {
            "orcid": identifier, "affiliations": [affiliation],
        }})
        self.db.raw.insert_one({
            "source": "orcid", "request": {"orcid": identifier},
            "fetched_at": datetime(2026, 11, 1, tzinfo=UTC),
            "payload": {"activities-summary": {"employments": {"affiliation-group": []}}},
        })
        before = self.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "plan"
            write_plan(self.db, out)
            output = json_util.loads((out / "persons.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(output["membership_status"], "unsupported")
            person = Person.model_validate(output)
            apply_membership(person)
            self.assertEqual(person.membership_status, "unsupported")
            self.assertFalse(person.is_itmo)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
