"""Reviewed membership migrations must preserve data and be safely resumable."""

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mongomock
from bson import ObjectId

from pauk.models import Person
from pauk.pipeline.membership import POLICY_VERSION
from scripts.reassess_membership import JOURNAL, _preserve_source_metadata, apply_plan, main, write_plan


class MembershipReassessmentApplyTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().membership_apply
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "plan"
        for person_id in ("A1", "A2"):
            self.db.persons.insert_one(
                {
                    "_id": ObjectId(),
                    "id": person_id,
                    "name_raw": person_id,
                    "is_itmo": True,
                    "groups": ["smoke", "full"],
                    "_version": 7,
                    "emails": [f"{person_id}@example.test"],
                    "extra": {"keep": [1, None]},
                }
            )
        self.before = list(self.db.persons.find({}).sort("id", 1))
        self.db.raw.insert_one({"source": "unrelated", "payload": {"keep": True}})
        with patch("scripts.reassess_membership.rebuild_from_global_sources", side_effect=self.rebuild):
            self.plan_id = write_plan(self.db, self.path)["plan_id"]

    @staticmethod
    def rebuild(db, person, **kwargs):
        result = person.model_copy(deep=True)
        result.is_itmo = False
        result.membership_status = "unsupported"
        result.membership_policy = POLICY_VERSION
        return result

    def test_dry_run_does_not_write_any_collection(self):
        names = self.db.list_collection_names()
        report = apply_plan(self.db, self.path, self.plan_id)
        self.assertEqual(report["changed"], 2)
        self.assertTrue(report["dry_run"])
        self.assertEqual(list(self.db.persons.find({}).sort("id", 1)), self.before)
        self.assertEqual(self.db.list_collection_names(), names)

    def test_apply_preserves_fields_groups_ids_and_full_revisions(self):
        report = apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertEqual(report["applied"], 2)
        for original in self.before:
            updated = self.db.persons.find_one({"_id": original["_id"]})
            for key in ("id", "_id", "groups", "emails", "extra", "name_raw"):
                self.assertEqual(updated[key], original[key])
            self.assertEqual(updated["_version"], 8)
            self.assertFalse(updated["is_itmo"])
            revision = self.db.revisions.find_one({"entity_id": original["id"]})
            self.assertEqual(revision["snapshot"], original)
        self.assertEqual(self.db[JOURNAL].count_documents({"state": "applied"}), 2)

    def test_rerun_is_idempotent(self):
        apply_plan(self.db, self.path, self.plan_id, apply=True)
        snapshot = list(self.db.persons.find({}))
        report = apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertEqual(report["already_applied"], 2)
        self.assertEqual(report["applied"], 0)
        self.assertEqual(list(self.db.persons.find({})), snapshot)
        self.assertEqual(self.db.revisions.count_documents({}), 2)

    def test_person_change_refuses_before_any_apply(self):
        self.db.persons.update_one({"id": "A2"}, {"$set": {"email": "later@example.test"}})
        with self.assertRaisesRegex(ValueError, "person changed"):
            apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertTrue(self.db.persons.find_one({"id": "A1"})["is_itmo"])
        self.assertEqual(self.db[JOURNAL].count_documents({}), 0)

    def test_source_change_refuses_before_any_apply(self):
        for collection in ("raw", "publications", "person_merge_sources"):
            with self.subTest(collection=collection):
                inserted = self.db[collection].insert_one({"new": True}).inserted_id
                with self.assertRaisesRegex(ValueError, "source collections changed"):
                    apply_plan(self.db, self.path, self.plan_id, apply=True)
                self.db[collection].delete_one({"_id": inserted})
        self.assertEqual(self.db[JOURNAL].count_documents({}), 0)

    def test_source_change_during_apply_reports_failure_before_publication(self):
        real_update = self.db.persons.update_one

        def mutate_source(query, update, *args, **kwargs):
            result = real_update(query, update, *args, **kwargs)
            self.db.raw.insert_one({"source": "changed", "payload": {}})
            return result

        with (
            patch.object(self.db.persons, "update_one", side_effect=mutate_source),
            self.assertRaisesRegex(ValueError, "do not publish"),
        ):
            apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertEqual(self.db[JOURNAL].count_documents({"state": "applied"}), 2)
        self.assertEqual(self.db.revisions.count_documents({}), 2)

    def test_cli_requires_explicit_reviewed_plan_and_target_before_connecting(self):
        invalid = [
            ["--mongo-uri", "mongodb://example.test", "--apply"],
            ["--mongo-uri", "mongodb://example.test", "--apply-plan", "plan", "--plan-id", "hash"],
            ["--mongo-uri", "mongodb://example.test", "--db", "pauk", "--apply-plan", "plan"],
            ["--snapshot-dir", "snapshot", "--apply-plan", "plan", "--plan-id", "hash"],
        ]
        for arguments in invalid:
            with (
                self.subTest(arguments=arguments),
                patch("sys.argv", ["reassess_membership", *arguments]),
                patch("scripts.reassess_membership.MongoClient") as client,
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit),
            ):
                main()
            client.assert_not_called()

    def test_new_person_refuses(self):
        self.db.persons.insert_one({"_id": "A3", "id": "A3"})
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            apply_plan(self.db, self.path, self.plan_id, apply=True)

    def test_wrong_plan_id_and_edited_file_refuse(self):
        with self.assertRaisesRegex(ValueError, "plan ID"):
            apply_plan(self.db, self.path, "wrong", apply=True)
        with (self.path / "persons.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(ValueError, "file changed"):
            apply_plan(self.db, self.path, self.plan_id, apply=True)

    def test_crash_after_person_update_resumes_revision_and_remaining_rows(self):
        with (
            patch("scripts.reassess_membership._finish_entry", side_effect=RuntimeError("interrupted")),
            self.assertRaisesRegex(RuntimeError, "interrupted"),
        ):
            apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertFalse(self.db.persons.find_one({"id": "A1"})["is_itmo"])
        self.assertTrue(self.db.persons.find_one({"id": "A2"})["is_itmo"])
        self.assertEqual(self.db[JOURNAL].count_documents({"state": "pending"}), 1)
        report = apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertEqual(report["already_applied"], 1)
        self.assertEqual(report["applied"], 1)
        self.assertEqual(self.db.revisions.count_documents({}), 2)

    def test_crash_before_person_update_resumes_from_backup(self):
        with (
            patch.object(self.db.persons, "update_one", side_effect=RuntimeError("interrupted")),
            self.assertRaises(RuntimeError),
        ):
            apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertEqual(list(self.db.persons.find({}).sort("id", 1)), self.before)
        report = apply_plan(self.db, self.path, self.plan_id, apply=True)
        self.assertEqual(report["applied"], 2)

    def test_compare_and_set_protects_new_fields_added_after_preflight(self):
        real_update = self.db.persons.update_one
        raced = False

        def race(query, update, *args, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                real_update({"id": "A1"}, {"$set": {"new_enrichment": "preserve"}})
            return real_update(query, update, *args, **kwargs)

        with (
            patch.object(self.db.persons, "update_one", side_effect=race),
            self.assertRaisesRegex(ValueError, "changed during apply"),
        ):
            apply_plan(self.db, self.path, self.plan_id, apply=True)
        person = self.db.persons.find_one({"id": "A1"})
        self.assertEqual(person["new_enrichment"], "preserve")
        self.assertTrue(person["is_itmo"])
        self.assertEqual(self.db.revisions.count_documents({}), 0)

    def test_unchanged_plan_does_not_increment_version(self):
        path = Path(self.temporary.name) / "unchanged"
        with patch("scripts.reassess_membership.rebuild_from_global_sources", side_effect=lambda db, p, **kw: p):
            report = write_plan(self.db, path)
        # The first plan also fills explicit default provenance fields; the next is a true no-op.
        apply_plan(self.db, path, report["plan_id"], apply=True)
        next_path = Path(self.temporary.name) / "no-op"
        with patch("scripts.reassess_membership.rebuild_from_global_sources", side_effect=lambda db, p, **kw: p):
            report = write_plan(self.db, next_path)
        before = list(self.db.persons.find({}))
        self.assertEqual(apply_plan(self.db, next_path, report["plan_id"], apply=True)["changed"], 0)
        self.assertEqual(list(self.db.persons.find({})), before)

    def test_plan_generation_refuses_changing_database(self):
        def changing(db, person, **kwargs):
            db.raw.insert_one({"source": "new", "payload": {}})
            return Person.model_validate(person)

        path = Path(self.temporary.name) / "changing"
        with (
            patch("scripts.reassess_membership.rebuild_from_global_sources", side_effect=changing),
            self.assertRaisesRegex(ValueError, "database changed"),
        ):
            write_plan(self.db, path)
        self.assertFalse((path / "manifest.json").exists())

    def test_added_authorship_provenance_preserves_unknown_fields(self):
        original = {"publication_id": "W1", "position": 1, "provider_metadata": {"keep": True}}
        updated = {
            "publication_id": "W1",
            "position": 1,
            "institution_source_person_id": "A1",
            "institution_source_id": "W1",
        }
        rows = _preserve_source_metadata([original], [updated], ("publication_id", "position"))
        self.assertEqual(rows[0]["provider_metadata"], {"keep": True})
        self.assertEqual(rows[0]["institution_source_person_id"], "A1")

    def test_ambiguous_legacy_authorship_refuses_metadata_loss(self):
        original = [{"publication_id": "W1", "position": 1, "provider_metadata": x} for x in (1, 2)]
        updated = {
            "publication_id": "W1",
            "position": 1,
            "institution_source_person_id": "A1",
            "institution_source_id": "W1",
            "institution_rors": [],
        }
        # Identical visible legacy rows can be carried in order; conflicting old facts cannot.
        original[0]["institution_rors"] = ["old"]
        original[1]["institution_rors"] = ["other"]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            _preserve_source_metadata(original, [updated], ("publication_id", "position"))


if __name__ == "__main__":
    unittest.main()
