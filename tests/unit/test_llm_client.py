import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from pauk.settings import Settings
from pauk.sources import llm
from pauk.sources.base import HttpRequestError
from pauk.sources.llm import (
    OPENROUTER_KEY_URL,
    ClaudeCli,
    CodexCli,
    GeminiCli,
    OpenRouterClient,
    SubscriptionCliClient,
    _run_cli,
    check_providers,
    kill_running_clis,
    llm_client,
)


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

    @patch("pauk.sources.base.time.sleep", return_value=None)
    def test_retries_a_network_failure_then_succeeds(self, sleep):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        client.session.post = MagicMock(
            side_effect=[requests.Timeout("temporary"), self.response(200, payload)]
        )

        self.assertEqual(client.chat_json("some prompt"), {"ok": True})
        self.assertEqual(client.session.post.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @patch("pauk.sources.base.time.sleep", return_value=None)
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

    @patch("pauk.sources.base.time.sleep", return_value=None)
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

    @patch("pauk.sources.base.time.sleep", return_value=None)
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

    @patch("pauk.sources.base.time.sleep", return_value=None)
    def test_long_rate_limit_wait_is_logged_without_the_key(self, sleep):
        client = OpenRouterClient(timeout=5, api_key="sk-or-SECRET", model="test-model")
        payload = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        client.session.post = MagicMock(
            side_effect=[
                self.response(429, {"error": {"message": "rate limited"}}, headers={"Retry-After": "30"}),
                self.response(200, payload),
            ]
        )

        with self.assertLogs("pauk.sources.base", level="WARNING") as logs:
            self.assertEqual(client.chat_json("some prompt"), {"ok": True})
        sleep.assert_called_once_with(30.0)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("waiting 30s", logs.output[0])
        self.assertIn("POST https://openrouter.ai/api/v1/chat/completions (HTTP 429", logs.output[0])
        self.assertNotIn("SECRET", logs.output[0])

    @patch("pauk.sources.base.time.sleep", return_value=None)
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
class ClaudeCliTest(unittest.TestCase):
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
        client = SubscriptionCliClient(5, ClaudeCli(), "haiku")
        seen = {}

        def run_cli(command, prompt, env, timeout, cwd):
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
        client = SubscriptionCliClient(5, ClaudeCli(), "haiku")

        with patch("pauk.sources.llm._run_cli") as run_cli:
            self.assertIsNone(client.chat_json("prompt"))
        run_cli.assert_not_called()
        self.assertEqual(client.unavailable_reason(), "claude CLI not found")


@patch("pauk.sources.llm.shutil.which", return_value="/usr/bin/codex")
class CodexCliTest(unittest.TestCase):
    EVENTS = [
        {"type": "thread.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 100, "output_tokens": 30}},
    ]

    def run_client(self, reply, returncode=0, events=EVENTS, stderr=""):
        client = SubscriptionCliClient(5, CodexCli(), "gpt-x")
        seen = {}

        def run_cli(command, prompt, env, timeout, cwd):
            seen.update(command=command, prompt=prompt, env=env)
            if reply is not None:
                Path(command[command.index("--output-last-message") + 1]).write_text(reply, encoding="utf-8")
            return returncode, "\n".join(json.dumps(event) for event in events), stderr

        with patch("pauk.sources.llm._run_cli", side_effect=run_cli):
            result = client.chat_json("user prompt", system_prompt="judge pairs")
        return client, result, seen

    def test_reads_the_last_message_and_token_usage(self, _which):
        client, result, seen = self.run_client('{"ok": true}')

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_usage, {"prompt_tokens": 900, "completion_tokens": 30})
        self.assertTrue(seen["prompt"].startswith("judge pairs"))
        self.assertTrue(seen["prompt"].endswith("user prompt"))
        self.assertEqual(seen["command"][-3:], ["--model", "gpt-x", "-"])
        for flag in ("--json", "--ephemeral", "--skip-git-repo-check", "--output-last-message"):
            self.assertIn(flag, seen["command"])
        self.assertEqual(seen["command"][seen["command"].index("--sandbox") + 1], "read-only")
        self.assertEqual(client.model, "codex-cli/gpt-x")

    def test_the_api_key_is_dropped_so_the_subscription_is_billed(self, _which):
        parent = {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://proxy", "CODEX_API_KEY": "sk-y", "KEEP": "1"}
        with patch.dict("os.environ", parent):
            _, _, seen = self.run_client('{"ok": true}')

        self.assertEqual({name for name in parent if name in seen["env"]}, {"KEEP"})
        self.assertEqual(seen["env"]["KEEP"], "1")

    def test_a_usage_limit_is_reported_as_such(self, _which):
        events = [{"type": "error", "message": "You've hit your usage limit. Try again later."}]
        client, result, _ = self.run_client(None, returncode=1, events=events)

        self.assertIsNone(result)
        self.assertTrue(client.last_error.startswith("subscription limit reached"))

    def test_a_failed_call_reports_stderr(self, _which):
        client, result, _ = self.run_client(None, returncode=2, events=[], stderr="Not logged in")

        self.assertIsNone(result)
        self.assertEqual(client.last_error, "CLI error: Not logged in")

    def test_an_empty_last_message_is_a_failure(self, _which):
        client, result, _ = self.run_client("")

        self.assertIsNone(result)
        self.assertEqual(client.last_error, "empty model response")

    def test_an_error_event_without_a_reply_is_classified_even_on_exit_zero(self, _which):
        events = [
            {"type": "turn.failed", "error": {"message": "exceeded retry limit, last status: 429 Too Many Requests"}}
        ]
        client, result, _ = self.run_client(None, events=events)

        self.assertIsNone(result)
        self.assertTrue(client.last_error.startswith("subscription limit reached"))

    def test_a_timeout_is_a_failure_not_a_crash(self, _which):
        client = SubscriptionCliClient(5, CodexCli(), "gpt-x")

        with patch("pauk.sources.llm._run_cli", side_effect=subprocess.TimeoutExpired("codex", 5)):
            self.assertIsNone(client.chat_json("prompt"))
        self.assertIn("timed out", client.last_error)


@patch("pauk.sources.llm.shutil.which", return_value="/usr/bin/gemini")
class GeminiCliTest(unittest.TestCase):
    @staticmethod
    def reply(response, **extra):
        stats = {"models": {"gemini-2.5-flash": {"tokens": {"prompt": 800, "candidates": 25, "thoughts": 5}}}}
        return {"session_id": "s", "response": response, "stats": stats, **extra}

    def run_client(self, outcome, returncode=0, stderr=""):
        client = SubscriptionCliClient(5, GeminiCli(), "flash")
        seen = {}

        def run_cli(command, prompt, env, timeout, cwd):
            seen.update(command=command, prompt=prompt, env=env, cwd=cwd)
            seen["system"] = (cwd / env["GEMINI_SYSTEM_MD"]).read_text(encoding="utf-8")
            return returncode, outcome if isinstance(outcome, str) else json.dumps(outcome, indent=2), stderr

        with patch("pauk.sources.llm._run_cli", side_effect=run_cli):
            result = client.chat_json("user prompt", system_prompt="judge pairs")
        return client, result, seen

    def test_reads_the_response_and_token_usage(self, _which):
        client, result, seen = self.run_client(self.reply('```json\n{"ok": true}\n```'))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_usage, {"prompt_tokens": 800, "completion_tokens": 30})
        self.assertEqual(seen["prompt"], "user prompt")
        self.assertTrue(seen["system"].startswith("judge pairs"))
        self.assertEqual(seen["command"][seen["command"].index("--model") + 1], "flash")
        self.assertEqual(seen["command"][seen["command"].index("--extensions") + 1], "none")
        self.assertEqual(client.model, "gemini-cli/flash")

    def test_keys_that_bill_an_api_are_dropped_and_the_login_is_used(self, _which):
        parent = {"GEMINI_API_KEY": "k", "GOOGLE_API_KEY": "k", "GOOGLE_GENAI_USE_VERTEXAI": "true", "KEEP": "1"}
        with patch.dict("os.environ", parent):
            _, _, seen = self.run_client(self.reply('{"ok": true}'))

        self.assertEqual({name for name in parent if name in seen["env"]}, {"KEEP"})
        self.assertEqual(seen["env"]["GOOGLE_GENAI_USE_GCA"], "true")

    def test_a_quota_error_is_reported_as_a_subscription_limit(self, _which):
        reply = {"error": {"type": "TerminalQuotaError", "message": "You have exhausted your daily quota."}}
        client, result, _ = self.run_client(reply, returncode=1)

        self.assertIsNone(result)
        self.assertTrue(client.last_error.startswith("subscription limit reached"))

    def test_another_error_keeps_its_message(self, _which):
        reply = {"error": {"type": "FatalAuthenticationError", "message": "Please set an Auth method"}}
        client, result, _ = self.run_client(reply, returncode=41)

        self.assertIsNone(result)
        self.assertEqual(client.last_error, "CLI error: Please set an Auth method")

    def test_output_that_is_not_json_is_a_failure_with_the_raw_text_kept(self, _which):
        client, result, _ = self.run_client("Loaded cached credentials.", returncode=1)

        self.assertIsNone(result)
        self.assertEqual(client.last_response["_raw_text"], "Loaded cached credentials.")
        self.assertIn("not JSON", client.last_error)

    def test_a_notice_printed_before_the_json_is_skipped(self, _which):
        client, result, _ = self.run_client("Loaded cached credentials.\n" + json.dumps(self.reply('{"ok": 1}')))

        self.assertEqual(result, {"ok": 1})


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

    def test_an_interrupted_stage_kills_the_clis_its_worker_threads_wait_on(self):
        outcome = {}

        def worker():
            try:
                outcome["result"] = _run_cli(
                    [sys.executable, "-c", "import time; time.sleep(60)"], "", dict(os.environ), 120
                )
            except BaseException as exc:
                outcome["result"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 30
        while not llm._running and time.monotonic() < deadline:
            time.sleep(0.05)
        started = time.monotonic()
        kill_running_clis()
        thread.join(30)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 30)
        self.assertNotEqual(outcome["result"][0], 0)
        self.assertEqual(llm._running, set())


class LlmProviderTest(unittest.TestCase):
    def test_provider_picks_the_client_and_model(self):
        self.assertIsInstance(llm_client("openrouter", timeout=5, api_key="k", openrouter_model="m"), OpenRouterClient)
        models = {
            provider: llm_client(provider, timeout=5, api_key="", openrouter_model="m").model
            for provider in ("claude", "claude:sonnet", "codex:gpt-x", "gemini:flash")
        }

        self.assertEqual(
            models,
            {
                "claude": "claude-cli/haiku",
                "claude:sonnet": "claude-cli/sonnet",
                "codex:gpt-x": "codex-cli/gpt-x",
                "gemini:flash": "gemini-cli/flash",
            },
        )
        for provider in ("claud", "codex", "gemini"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                llm_client(provider, timeout=5, api_key="", openrouter_model="m")

    def test_a_provider_typo_is_caught_before_any_stage_runs(self):
        check_providers(Settings(llm_provider="claude", person_resolution_provider="codex:gpt-x"))

        with self.assertRaisesRegex(ValueError, "^person_resolution: unknown LLM provider 'claud'"):
            check_providers(Settings(person_resolution_provider="claud"))
        with self.assertRaisesRegex(ValueError, "^author_names: codex needs a model"):
            check_providers(Settings(author_names_provider="codex"))

    def test_a_stage_setting_wins_and_dedup_switches_only_by_its_own(self):
        config = Settings(llm_provider="claude", author_names_provider="openrouter", person_resolution_provider="")

        self.assertEqual(config.provider_for("author_names"), "openrouter")
        self.assertEqual(config.provider_for("link_relevance"), "claude")
        self.assertEqual(config.provider_for("person_resolution"), "openrouter")


class OpenRouterClientUsageTest(unittest.TestCase):
    def setUp(self):
        self.client = OpenRouterClient(timeout=5, api_key="secret-key", model="test-model")
        patcher = patch("pauk.sources.base.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_usage_reads_the_running_total_of_the_key_with_the_key_as_bearer(self):
        self.client.session.request = MagicMock(
            return_value=OpenRouterClientLastResponseTest.response(200, {"data": {"usage": 1.25}})
        )
        self.assertEqual(self.client.usage(), 1.25)
        self.assertEqual(self.client.session.request.call_args.args, ("GET", OPENROUTER_KEY_URL))
        self.assertEqual(self.client.session.headers["Authorization"], "Bearer secret-key")

    def test_usage_retries_a_transient_error(self):
        self.client.session.request = MagicMock(side_effect=[
            OpenRouterClientLastResponseTest.response(502, {}),
            OpenRouterClientLastResponseTest.response(200, {"data": {"usage": 2.0}}),
        ])
        self.assertEqual(self.client.usage(), 2.0)

    def test_usage_raises_without_the_key_in_the_message(self):
        self.client.session.request = MagicMock(return_value=OpenRouterClientLastResponseTest.response(401, {}))
        with self.assertRaises(HttpRequestError) as caught:
            self.client.usage()
        self.assertEqual(caught.exception.status_code, 401)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_usage_reports_a_network_failure_by_its_type(self):
        self.client.session.request = MagicMock(side_effect=requests.ConnectionError("down"))
        with self.assertRaisesRegex(HttpRequestError, "ConnectionError"):
            self.client.usage()
