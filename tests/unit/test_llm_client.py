import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from pauk.settings import Settings
from pauk.sources.llm import ClaudeCliClient, OpenRouterClient, _run_cli, llm_client


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


@patch("pauk.sources.llm.shutil.which", return_value="/usr/bin/claude")
class ClaudeCliClientTest(unittest.TestCase):
    @staticmethod
    def reply(result, **extra):
        return {
            "type": "result",
            "is_error": False,
            "result": result,
            "total_cost_usd": 0.002,
            "usage": {"input_tokens": 700, "cache_read_input_tokens": 50, "output_tokens": 20},
            **extra,
        }

    def run_client(self, outcome, returncode=0, **kwargs):
        client = ClaudeCliClient(timeout=5)
        seen = {}

        def run_cli(command, prompt, env, timeout):
            seen.update(command=command, prompt=prompt, env=env)
            seen["system"] = Path(command[command.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
            if isinstance(outcome, Exception):
                raise outcome
            return returncode, outcome if isinstance(outcome, str) else json.dumps(outcome), ""

        with patch("pauk.sources.llm._run_cli", side_effect=run_cli):
            result = client.chat_json("user prompt", **kwargs)
        return client, result, seen

    def test_parses_a_fenced_reply_and_maps_usage(self, _which):
        client, result, seen = self.run_client(self.reply('```json\n{"ok": true}\n```'))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_usage, {"prompt_tokens": 750, "completion_tokens": 20, "cost": 0.002})
        self.assertEqual(seen["prompt"], "user prompt")
        self.assertIn("--safe-mode", seen["command"])
        self.assertEqual(client.model, "claude-cli/haiku")

    def test_system_prompt_goes_through_a_file_and_the_file_is_removed(self, _which):
        _, _, seen = self.run_client(self.reply('{"ok": true}'), system_prompt="judge pairs")

        self.assertTrue(seen["system"].startswith("judge pairs"))
        self.assertFalse(Path(seen["command"][seen["command"].index("--system-prompt-file") + 1]).exists())

    def test_variables_that_change_billing_or_cap_the_reply_are_dropped(self, _which):
        parent = {
            "ANTHROPIC_API_KEY": "sk-ant",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://proxy",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "10",
            "KEEP": "1",
        }
        with patch.dict("os.environ", parent):
            _, _, seen = self.run_client(self.reply('{"ok": true}'), max_tokens=10)

        self.assertEqual({name for name in parent if name in seen["env"]}, {"KEEP"})
        self.assertEqual(seen["env"]["MAX_THINKING_TOKENS"], "0")

    def test_a_usage_limit_is_reported_as_such(self, _which):
        reply = self.reply("Claude AI usage limit reached|1760600000", is_error=True)
        client, result, _ = self.run_client(reply, returncode=1)

        self.assertIsNone(result)
        self.assertTrue(client.last_error.startswith("subscription limit reached"))

    def test_output_that_is_not_json_is_a_failure_with_the_raw_text_kept(self, _which):
        client, result, _ = self.run_client("Not logged in", returncode=1)

        self.assertIsNone(result)
        self.assertEqual(client.last_response["_raw_text"], "Not logged in")
        self.assertIn("not JSON", client.last_error)

    def test_a_non_object_reply_is_a_failure(self, _which):
        client, result, _ = self.run_client(self.reply("[1, 2]"))

        self.assertIsNone(result)
        self.assertIn("not an object", client.last_error)

    def test_a_timeout_is_a_failure_not_a_crash(self, _which):
        client, result, _ = self.run_client(subprocess.TimeoutExpired("claude", 5))

        self.assertIsNone(result)
        self.assertIn("timed out", client.last_error)

    def test_a_missing_cli_makes_no_call(self, which):
        which.return_value = None
        client = ClaudeCliClient(timeout=5)

        with patch("pauk.sources.llm._run_cli") as run_cli:
            self.assertIsNone(client.chat_json("prompt"))
        run_cli.assert_not_called()
        self.assertEqual(client.unavailable_reason(), "claude CLI not found")


class RunCliTest(unittest.TestCase):
    def test_a_hung_process_is_killed_at_the_timeout(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_cli([sys.executable, "-c", "import time; time.sleep(60)"], "", dict(os.environ), 1)

        self.assertLess(time.monotonic() - started, 30)

    def test_output_that_is_not_utf8_is_decoded_not_raised(self):
        code = "import sys; sys.stdout.buffer.write(bytes([0xff, 0x41]))"

        returncode, stdout, _ = _run_cli([sys.executable, "-c", code], "", dict(os.environ), 30)

        self.assertEqual((returncode, stdout), (0, bytes([0xFF, 0x41]).decode("utf-8", "replace")))


class LlmProviderTest(unittest.TestCase):
    def test_provider_picks_the_client_and_model(self):
        self.assertIsInstance(llm_client("openrouter", timeout=5, api_key="k", openrouter_model="m"), OpenRouterClient)
        self.assertEqual(llm_client("claude", timeout=5, api_key="", openrouter_model="m").model, "claude-cli/haiku")
        self.assertEqual(
            llm_client("claude:sonnet", timeout=5, api_key="", openrouter_model="m").model, "claude-cli/sonnet"
        )
        with self.assertRaises(ValueError):
            llm_client("codex", timeout=5, api_key="", openrouter_model="m")

    def test_a_stage_setting_wins_and_dedup_switches_only_by_its_own(self):
        config = Settings(llm_provider="claude", author_names_provider="openrouter", person_resolution_provider="")

        self.assertEqual(config.provider_for("author_names"), "openrouter")
        self.assertEqual(config.provider_for("link_relevance"), "claude")
        self.assertEqual(config.provider_for("person_resolution"), "openrouter")
