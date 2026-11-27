"""Unit tests for Mongo link suggestion queue (`pauk.storage.link_suggestions`)."""

import unittest
from datetime import timedelta
from unittest.mock import patch

import mongomock
from pymongo.errors import DuplicateKeyError, OperationFailure

from pauk.storage import link_suggestions


class LinkSuggestionsStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = mongomock.MongoClient()["pauk_suggestions_test"]

    def test_dedup_key_and_content_hash_determinism(self) -> None:
        k1 = link_suggestions.dedup_key(
            "W1", link_suggestions.IMPLEMENTS, repository_url="https://github.com/org/repo"
        )
        k2 = link_suggestions.dedup_key(
            "W1", link_suggestions.IMPLEMENTS, repository_url="https://github.com/org/repo"
        )
        k3 = link_suggestions.dedup_key(
            "W1", link_suggestions.MENTIONS, repository_url="https://github.com/org/repo"
        )
        k_ft = link_suggestions.dedup_key(
            "W1", link_suggestions.FULL_TEXT, evidence_url="https://arxiv.org/pdf/2501.00001.pdf"
        )
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertNotEqual(k1, k_ft)
        with self.assertRaises(link_suggestions.SuggestionError):
            link_suggestions.dedup_key("W1", "bogus", repository_url="https://github.com/org/repo")
        with self.assertRaises(link_suggestions.SuggestionError):
            link_suggestions.dedup_key("W1", link_suggestions.FULL_TEXT)
        with self.assertRaises(link_suggestions.SuggestionError):
            link_suggestions.dedup_key("W1", link_suggestions.IMPLEMENTS)

        h1 = link_suggestions.content_hash(k1, "Author README cites paper", quote="Our code is at...")
        h2 = link_suggestions.content_hash(k1, "Author README cites paper", quote="Our code is at...")
        h3 = link_suggestions.content_hash(k1, "Different rationale", quote="Our code is at...")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)

    def test_submit_dedup_prior_decision_and_queue_caps(self) -> None:
        link_suggestions.ensure_indexes(self.db)
        first = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/org/repo",
            rationale="Found official repo mentioned in paper appendix.",
            quote="Code available at github.com/org/repo",
        )
        self.assertEqual(first["status"], "queued")
        self.assertEqual(link_suggestions.count(self.db, status=link_suggestions.PENDING), 1)

        dup = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/org/repo",
            rationale="Second agent with slightly different wording",
        )
        self.assertEqual(dup["status"], "already_queued")
        self.assertEqual(dup["suggestion_id"], first["suggestion_id"])
        self.assertEqual(dup["document"]["submission_count"], 2)

        rejected = link_suggestions.reject(self.db, first["suggestion_id"], actor="user:roman", note="Not authored")
        self.assertEqual(rejected["status"], link_suggestions.REJECTED)
        self.assertEqual(link_suggestions.count(self.db, status=link_suggestions.PENDING), 0)
        self.assertEqual(link_suggestions.count(self.db, status=link_suggestions.REJECTED), 1)

        replay = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/org/repo",
            rationale="Found official repo mentioned in paper appendix.",
            quote="Code available at github.com/org/repo",
        )
        self.assertEqual(replay["status"], "already_reviewed")
        self.assertEqual(replay["suggestion_id"], first["suggestion_id"])

        reopened = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/org/repo",
            rationale="Found author's confirmation in paper README on GitHub.",
            evidence_url="https://github.com/org/repo/blob/main/README.md",
        )
        self.assertEqual(reopened["status"], "queued")
        self.assertNotEqual(reopened["suggestion_id"], first["suggestion_id"])

        priors = link_suggestions.prior_decisions(self.db, [reopened["document"]["dedup_key"]])
        self.assertEqual(priors[reopened["document"]["dedup_key"]]["_id"], first["suggestion_id"])

        link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.MENTIONS,
            repository_url="https://github.com/org/other",
            rationale="Second distinct pending item for W1.",
            max_per_publication=2,
            max_pending=5,
        )
        with self.assertRaises(link_suggestions.SuggestionQueueFull):
            link_suggestions.submit(
                self.db,
                publication_id="W1",
                proposed_relation=link_suggestions.FULL_TEXT,
                evidence_url="https://arxiv.org/pdf/2501.00001.pdf",
                rationale="Third pending item for W1 exceeds per-publication cap.",
                max_per_publication=2,
                max_pending=5,
            )
        with self.assertRaises(link_suggestions.SuggestionQueueFull):
            link_suggestions.submit(
                self.db,
                publication_id="W2",
                proposed_relation=link_suggestions.FULL_TEXT,
                evidence_url="https://arxiv.org/pdf/2501.00002.pdf",
                rationale="Global cap exceeded.",
                max_per_publication=5,
                max_pending=2,
            )

    def test_claim_lease_release_approval_and_ordering(self) -> None:
        s1 = link_suggestions.submit(
            self.db,
            publication_id="W1",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r1",
            rationale="First submission in queue.",
        )
        s2 = link_suggestions.submit(
            self.db,
            publication_id="W2",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_id="r2",
            rationale="Second submission in queue.",
        )
        listed = link_suggestions.list_suggestions(self.db, status=link_suggestions.PENDING)
        self.assertEqual([row["_id"] for row in listed], [s1["suggestion_id"], s2["suggestion_id"]])

        claimed = link_suggestions.claim_for_apply(self.db, s1["suggestion_id"], "user:editor")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], link_suggestions.APPLYING)
        self.assertTrue(claimed["active"])
        self.assertIsNone(link_suggestions.claim_for_apply(self.db, s1["suggestion_id"], "user:other"))
        self.assertIsNone(link_suggestions.reject(self.db, s1["suggestion_id"], actor="user:other"))
        self.assertFalse(link_suggestions.is_recoverable(claimed))
        self.assertIsNone(link_suggestions.claim_for_recovery(self.db, s1["suggestion_id"], "user:other"))

        self.db[link_suggestions.COLLECTION].update_one(
            {"_id": s1["suggestion_id"]},
            {"$set": {"applying_at": claimed["applying_at"] - timedelta(seconds=link_suggestions.APPLYING_LEASE_SECONDS + 10)}},
        )
        self.assertIsNone(link_suggestions.claim_for_apply(self.db, s1["suggestion_id"], "user:editor2"))
        self.assertIsNone(link_suggestions.reject(self.db, s1["suggestion_id"], actor="user:editor2"))
        reclaimed = link_suggestions.claim_for_recovery(self.db, s1["suggestion_id"], "user:editor2")
        self.assertIsNotNone(reclaimed)
        self.assertNotEqual(reclaimed["apply_token"], claimed["apply_token"])

        self.assertTrue(link_suggestions.release_claim(self.db, s1["suggestion_id"], reclaimed["apply_token"]))
        released_doc = link_suggestions.get(self.db, s1["suggestion_id"])
        self.assertEqual(released_doc["status"], link_suggestions.PENDING)
        self.assertTrue(released_doc["active"])

        final_claim = link_suggestions.claim_for_apply(self.db, s1["suggestion_id"], "user:editor")
        approved = link_suggestions.complete_approval(
            self.db,
            s1["suggestion_id"],
            final_claim["apply_token"],
            actor="user:editor",
            note="Verified in README",
            applied_effect={"action": "implements"},
        )
        self.assertEqual(approved["status"], link_suggestions.APPROVED)
        self.assertFalse(approved["active"])
        self.assertEqual(link_suggestions.count(self.db, status=link_suggestions.APPROVED), 1)

        c2 = link_suggestions.claim_for_apply(self.db, s2["suggestion_id"], "user:crashed")
        self.assertIsNotNone(c2)
        self.assertTrue(link_suggestions.mark_needs_recovery(self.db, s2["suggestion_id"], c2["apply_token"], reason="uncertain"))
        self.assertTrue(link_suggestions.is_recoverable(link_suggestions.get(self.db, s2["suggestion_id"])))
        self.assertIsNone(link_suggestions.reject(self.db, s2["suggestion_id"], actor="user:editor"))
        self.db[link_suggestions.COLLECTION].update_one(
            {"_id": s2["suggestion_id"]},
            {"$set": {"applying_at": c2["applying_at"] - timedelta(seconds=link_suggestions.APPLYING_LEASE_SECONDS + 10)}},
        )
        self.assertIsNone(link_suggestions.reject(self.db, s2["suggestion_id"], actor="user:editor"))
        recovered_c2 = link_suggestions.claim_for_recovery(self.db, s2["suggestion_id"], "user:editor")
        self.assertIsNotNone(recovered_c2)
        self.assertTrue(link_suggestions.release_claim(self.db, s2["suggestion_id"], recovered_c2["apply_token"]))
        rejected_after_recover = link_suggestions.reject(
            self.db, s2["suggestion_id"], actor="user:editor", note="Recovered then rejected"
        )
        self.assertIsNotNone(rejected_after_recover)
        self.assertEqual(rejected_after_recover["status"], link_suggestions.REJECTED)
        self.assertFalse(rejected_after_recover["active"])
        self.assertNotIn("apply_token", rejected_after_recover)

    def test_ensure_indexes_uses_active_boolean_filter_and_fails_loudly(self) -> None:
        link_suggestions.ensure_indexes(self.db)
        indexes = self.db[link_suggestions.COLLECTION].index_information()
        self.assertEqual(indexes["uniq_active_dedup_key"]["partialFilterExpression"], {"active": True})
        self.assertTrue(indexes["uniq_active_dedup_key"]["unique"])

        collection = self.db[link_suggestions.COLLECTION]
        with (
            patch.object(collection, "create_index", side_effect=OperationFailure("index failed")),
            self.assertRaises(OperationFailure),
        ):
            link_suggestions.ensure_indexes(self.db)

    def test_submit_handles_concurrent_duplicate_key_error(self) -> None:
        s = link_suggestions.submit(
            self.db,
            publication_id="W_RACE",
            proposed_relation=link_suggestions.IMPLEMENTS,
            repository_url="https://github.com/org/race",
            rationale="First winner of race.",
        )
        collection = self.db[link_suggestions.COLLECTION]
        key = s["document"]["dedup_key"]
        original_find_one_and_update = collection.find_one_and_update
        calls = {"count": 0}

        def race_find_one_and_update(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return None  # Pretend concurrent insert happened between first check and insert_one.
            return original_find_one_and_update(*args, **kwargs)

        with (
            patch.object(collection, "find_one_and_update", side_effect=race_find_one_and_update),
            patch.object(collection, "insert_one", side_effect=DuplicateKeyError("dup")),
        ):
            raced = link_suggestions.submit(
                self.db,
                publication_id="W_RACE",
                proposed_relation=link_suggestions.IMPLEMENTS,
                repository_url="https://github.com/org/race",
                rationale="Second concurrent submission.",
            )
        self.assertEqual(raced["status"], "already_queued")
        self.assertEqual(raced["suggestion_id"], s["suggestion_id"])
        self.assertEqual(raced["document"]["dedup_key"], key)
        self.assertEqual(raced["document"]["submission_count"], 2)
