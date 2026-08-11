import unittest

import mongomock

from pauk.storage.llm_log import LlmLogStore


class LlmLogStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_record_inserts_one_document_with_the_given_fields(self):
        store = LlmLogStore(self.db, "llm_logs_link_relevance")
        store.record(
            group="sample", model="anthropic/claude-haiku-4.5", prompt="is this the author's code?",
            raw_response={"choices": []}, parsed={"is_authors_artifact": True},
            usage={"total_tokens": 42}, error=None,
            context={"publication_id": "W1", "url": "https://github.com/org/repo"},
        )
        [doc] = list(self.db["llm_logs_link_relevance"].find({}))
        self.assertEqual(doc["group"], "sample")
        self.assertEqual(doc["model"], "anthropic/claude-haiku-4.5")
        self.assertEqual(doc["prompt"], "is this the author's code?")
        self.assertEqual(doc["raw_response"], {"choices": []})
        self.assertEqual(doc["parsed"], {"is_authors_artifact": True})
        self.assertEqual(doc["usage"], {"total_tokens": 42})
        self.assertIsNone(doc["error"])
        self.assertEqual(doc["context"], {"publication_id": "W1", "url": "https://github.com/org/repo"})
        self.assertIn("called_at", doc)
