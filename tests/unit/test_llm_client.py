import unittest
from unittest.mock import MagicMock, patch

import requests

from pauk.sources.llm import OpenRouterClient


class OpenRouterClientLastResponseTest(unittest.TestCase):
    @staticmethod
    def response(status, payload, *, headers=None, text=""):
        response = MagicMock()
        response.status_code = status
        response.headers = headers or {}
        response.text = text
        response.json.return_value = payload
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(response=response)
        else:
            response.raise_for_status.return_value = None
        return response

    def test_last_response_holds_the_raw_reply_after_a_successful_call(self):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        payload = {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"total_tokens": 10},
        }
        response = self.response(200, payload)
        client.session.post = MagicMock(return_value=response)

        result = client.chat_json("some prompt")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_response, payload)

    def test_last_response_is_none_when_no_api_key(self):
        client = OpenRouterClient(timeout=5, api_key="", model="test-model")
        client.chat_json("some prompt")
        self.assertIsNone(client.last_response)

    def test_system_prompt_and_max_tokens_are_kept_with_retry_capable_request(self):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        client.session.post = MagicMock(return_value=self.response(200, payload))

        result = client.chat_json(
            "user prompt",
            system_prompt="system prompt",
            max_tokens=321,
        )

        self.assertEqual(result, {"ok": True})
        body = client.session.post.call_args.kwargs["json"]
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user prompt"},
            ],
        )
        self.assertEqual(body["max_tokens"], 321)

    @patch("pauk.sources.llm.time.sleep", return_value=None)
    def test_retries_a_network_failure_then_succeeds(self, sleep):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        client.session.post = MagicMock(
            side_effect=[requests.Timeout("temporary"), self.response(200, payload)]
        )

        self.assertEqual(client.chat_json("some prompt"), {"ok": True})
        self.assertEqual(client.session.post.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @patch("pauk.sources.llm.time.sleep", return_value=None)
    def test_retries_a_transient_status_then_succeeds(self, sleep):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        client.session.post = MagicMock(
            side_effect=[
                self.response(503, {"error": {"message": "busy"}}),
                self.response(200, payload),
            ]
        )

        self.assertEqual(client.chat_json("some prompt"), {"ok": True})
        self.assertEqual(client.session.post.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @patch("pauk.sources.llm.time.sleep", return_value=None)
    def test_stops_after_three_transient_http_attempts(self, sleep):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        responses = [
            self.response(503, {"error": {"message": f"busy {attempt}"}})
            for attempt in range(1, 4)
        ]
        client.session.post = MagicMock(side_effect=responses)

        self.assertIsNone(client.chat_json("some prompt"))
        self.assertEqual(client.session.post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])
        self.assertEqual(client.last_response, {"error": {"message": "busy 3"}})
        self.assertEqual(client.last_error, "HTTP 503")

    @patch("pauk.sources.llm.time.sleep", return_value=None)
    def test_honors_retry_after_for_rate_limits(self, sleep):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        client.session.post = MagicMock(
            side_effect=[
                self.response(429, {"error": {"message": "rate limited"}}, headers={"Retry-After": "3"}),
                self.response(200, payload),
            ]
        )

        self.assertEqual(client.chat_json("some prompt"), {"ok": True})
        sleep.assert_called_once_with(3.0)

    @patch("pauk.sources.llm.time.sleep", return_value=None)
    def test_does_not_retry_a_non_transient_client_error(self, sleep):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        client.session.post = MagicMock(
            return_value=self.response(400, {"error": {"message": "bad request"}})
        )

        self.assertIsNone(client.chat_json("some prompt"))
        self.assertEqual(client.session.post.call_count, 1)
        self.assertEqual(client.last_error, "HTTP 400")
        sleep.assert_not_called()

    def test_a_non_object_response_envelope_is_a_failure_not_a_crash(self):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        client.session.post = MagicMock(return_value=self.response(200, []))

        self.assertIsNone(client.chat_json("some prompt"))
        self.assertEqual(client.last_response, [])
        self.assertIn("not an object", client.last_error)
