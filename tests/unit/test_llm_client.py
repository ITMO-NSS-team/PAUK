import unittest
from unittest.mock import MagicMock

from pauk.sources.llm import OpenRouterClient


class OpenRouterClientLastResponseTest(unittest.TestCase):
    def test_last_response_holds_the_raw_reply_after_a_successful_call(self):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        payload = {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"total_tokens": 10},
        }
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        client.session.post = MagicMock(return_value=response)

        result = client.chat_json("some prompt")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_response, payload)

    def test_last_response_is_none_when_no_api_key(self):
        client = OpenRouterClient(timeout=5, api_key="", model="test-model")
        client.chat_json("some prompt")
        self.assertIsNone(client.last_response)
