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

from pauk import cli
from pauk.settings import Settings
from pauk.sources import llm
from pauk.sources.base import HttpRequestError
from pauk.sources.llm import (
    OPENROUTER_KEY_URL,
    ClaudeCli,
    CliStoppedError,
    CliUnusableError,
    CodexCli,
    OpenRouterClient,
    SubscriptionCliClient,
    SubscriptionLimitError,
    _parse_json_body,
    _run_cli,
    check_providers,
    kill_running_clis,
    llm_client,
    resume_clis,
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

    def test_a_reconsidered_answer_is_read_from_the_last_block(self):
        client = OpenRouterClient(timeout=5, api_key="key", model="test-model")
        content = '```json\n{"ok": false}\n```\nWait, let me reconsider.\n```json\n{"ok": true}\n```'
        client.session.post = MagicMock(return_value=self.response(200, {"choices": [{"message": {"content": content}}]}))

        self.assertEqual(client.chat_json("some prompt"), {"ok": True})
        self.assertIsNone(client.last_error)

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


class ParseJsonBodyTest(unittest.TestCase):
    def test_a_clean_reply_parses_as_is(self):
        self.assertEqual(_parse_json_body('{"a": 1}'), {"a": 1})
        self.assertEqual(_parse_json_body('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(_parse_json_body("[1, 2]"), [1, 2])

    def test_a_second_fenced_answer_wins(self):
        reply = '```json\n{"a": 1}\n```\nWait, let me reconsider.\n```json\n{"a": 2}\n```'

        self.assertEqual(_parse_json_body(reply), {"a": 2})

    def test_a_second_bare_answer_wins(self):
        self.assertEqual(_parse_json_body('{"a": 1}\nWait, let me reconsider.\n{"a": 2}'), {"a": 2})

    def test_prose_after_the_object_is_ignored(self):
        self.assertEqual(_parse_json_body('{"a": {"b": 1}}\nThe name is Portuguese.'), {"a": {"b": 1}})
        self.assertEqual(_parse_json_body('```json\n{"a": 1}\n```\nNote: initials kept.'), {"a": 1})

    def test_braces_inside_strings_do_not_split_the_object(self):
        self.assertEqual(_parse_json_body('Answer: {"a": "x { y } z"} done'), {"a": "x { y } z"})

    def test_a_truncated_second_answer_leaves_the_first(self):
        self.assertEqual(_parse_json_body('{"a": 1}\nWait.\n{"a": 2, "b": '), {"a": 1})

    def test_a_reply_without_an_object_still_raises(self):
        for reply in ("Sorry, I cannot help { with that", "[1, 2] and more"):
            with self.subTest(reply=reply), self.assertRaises(json.JSONDecodeError):
                _parse_json_body(reply)


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
            "CLAUDE_CODE_OAUTH_TOKEN": "needed",
            "TEMP": "/tmp",
        }
        with patch.dict("os.environ", parent):
            _, _, seen = self.run_client(self.reply('{"ok": true}'), max_tokens=10)

        self.assertEqual({name for name in parent if name in seen["env"]}, {"CLAUDE_CODE_OAUTH_TOKEN", "TEMP"})
        self.assertEqual(seen["env"]["MAX_THINKING_TOKENS"], "0")

    def test_project_secrets_do_not_reach_the_cli(self, _which):
        parent = {
            "OPENROUTER_API_KEY": "k",
            "GITHUB_TOKEN": "k",
            "NEO4J_PASSWORD": "k",
            "MONGO_URI": "mongodb://u:p@h",
            "OPENALEX_API_KEY": "k",
        }
        with patch.dict("os.environ", parent):
            _, _, seen = self.run_client(self.reply('{"ok": true}'))

        self.assertEqual({name for name in parent if name in seen["env"]}, set())

    def test_a_usage_limit_stops_the_run(self, _which):
        for message in (
            "Claude AI usage limit reached|1760600000",
            "You've hit your limit \u00b7 resets 3pm",
            "You've hit your monthly spend limit.",
        ):
            with self.subTest(message=message), self.assertRaisesRegex(SubscriptionLimitError, "limit reached"):
                self.run_client(self.reply(message, is_error=True), returncode=1)

    def test_a_bare_429_stops_the_run_but_is_not_called_a_spent_subscription(self, _which):
        reply = self.reply("Request failed", is_error=True, api_error_status=429)
        with self.assertRaisesRegex(SubscriptionLimitError, "^rate limited"):
            self.run_client(reply, returncode=1)

    def test_a_signed_out_cli_stops_the_run(self, _which):
        for reply, returncode in (
            (self.reply("Not logged in \u00b7 Please run /login", is_error=True), 1),
            (self.reply("Request failed", is_error=True, api_error_status=401), 1),
            ("Not logged in", 1),
        ):
            with self.subTest(reply=str(reply)[:40]), self.assertRaises(CliUnusableError):
                self.run_client(reply, returncode=returncode)

    def test_a_run_of_failed_calls_stops_the_run_whatever_the_wording(self, _which):
        client = SubscriptionCliClient(5, ClaudeCli(), "haiku")
        failed = json.dumps(self.reply("Something unknown went wrong", is_error=True))

        with patch("pauk.sources.llm._run_cli", return_value=(1, failed, "")):
            for _ in range(19):
                self.assertIsNone(client.chat_json("prompt"))
            with self.assertRaisesRegex(CliUnusableError, "20 CLI calls failed in a row"):
                client.chat_json("prompt")

    def test_a_reply_from_the_model_resets_the_count_of_failures(self, _which):
        client = SubscriptionCliClient(5, ClaudeCli(), "haiku")
        failed = (1, json.dumps(self.reply("Something unknown went wrong", is_error=True)), "")
        answered = (0, json.dumps(self.reply("not json at all")), "")

        with patch("pauk.sources.llm._run_cli", side_effect=[failed] * 19 + [answered] + [failed] * 19):
            for _ in range(39):
                self.assertIsNone(client.chat_json("prompt"))

    def test_digits_inside_a_longer_token_are_not_a_limit(self, _which):
        client, result, _ = self.run_client(self.reply("failed: req_a1429f", is_error=True), returncode=1)

        self.assertIsNone(result)
        self.assertEqual(client.last_error, "CLI error: failed: req_a1429f")

    def test_a_usage_field_of_the_wrong_shape_is_tolerated(self, _which):
        client, result, _ = self.run_client(self.reply('{"ok": true}', usage=["odd"]))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_usage["prompt_tokens"], 0)

    def test_output_that_is_not_json_is_a_failure_with_the_raw_text_kept(self, _which):
        client, result, _ = self.run_client("Something broke", returncode=1)

        self.assertIsNone(result)
        self.assertEqual(client.last_response["_raw_text"], "Something broke")
        self.assertIn("not JSON", client.last_error)

    def test_prose_after_the_json_does_not_fail_the_call(self, _which):
        client, result, _ = self.run_client(self.reply('{"ok": true}\n\nI kept the initials as given.'))

        self.assertEqual(result, {"ok": True})
        self.assertIsNone(client.last_error)

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
        self.assertIn("features.shell_tool=false", seen["command"])
        self.assertIn("features.unified_exec=false", seen["command"])
        self.assertEqual(client.model, "codex-cli/gpt-x")

    def test_the_api_key_is_dropped_so_the_subscription_is_billed(self, _which):
        parent = {
            "OPENAI_API_KEY": "sk-x",
            "OPENAI_BASE_URL": "https://proxy",
            "CODEX_API_KEY": "sk-y",
            "CODEX_ACCESS_TOKEN": "t",
            "CODEX_HOME": "/home/me/.codex",
            "TEMP": "/tmp",
        }
        with patch.dict("os.environ", parent):
            _, _, seen = self.run_client('{"ok": true}')

        self.assertEqual({name for name in parent if name in seen["env"]}, {"CODEX_HOME", "TEMP"})

    def test_a_usage_limit_stops_the_run(self, _which):
        events = [{"type": "error", "message": "You\u2019ve hit your usage limit. Try again later."}]
        with self.assertRaisesRegex(SubscriptionLimitError, "limit reached"):
            self.run_client(None, returncode=1, events=events)

    def test_a_failed_call_reports_stderr(self, _which):
        client, result, _ = self.run_client(None, returncode=2, events=[], stderr="Something broke")

        self.assertIsNone(result)
        self.assertEqual(client.last_error, "CLI error: Something broke")

    def test_a_lost_login_stops_the_run(self, _which):
        for stderr in ("Not logged in", "Your access token could not be refreshed. Please log out and sign in again."):
            with self.subTest(stderr=stderr), self.assertRaises(CliUnusableError):
                self.run_client(None, returncode=1, events=[], stderr=stderr)

    def test_an_empty_last_message_is_a_failure(self, _which):
        client, result, _ = self.run_client("")

        self.assertIsNone(result)
        self.assertEqual(client.last_error, "empty model response")

    def test_an_error_event_without_a_reply_is_classified_even_on_exit_zero(self, _which):
        events = [
            {"type": "turn.failed", "error": {"message": "exceeded retry limit, last status: 429 Too Many Requests"}}
        ]
        with self.assertRaisesRegex(SubscriptionLimitError, "^rate limited"):
            self.run_client(None, events=events)

    def test_a_timeout_is_a_failure_not_a_crash(self, _which):
        client = SubscriptionCliClient(5, CodexCli(), "gpt-x")

        with patch("pauk.sources.llm._run_cli", side_effect=subprocess.TimeoutExpired("codex", 5)):
            self.assertIsNone(client.chat_json("prompt"))
        self.assertIn("timed out", client.last_error)


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
        self.addCleanup(resume_clis)
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

    def test_a_retry_after_the_kill_starts_nothing_until_the_stage_is_done(self):
        self.addCleanup(resume_clis)
        kill_running_clis()

        with patch("pauk.sources.llm.subprocess.Popen") as popen, self.assertRaises(CliStoppedError):
            _run_cli(["cli"], "", {}, 5)
        popen.assert_not_called()

        resume_clis()
        returncode, _, _ = _run_cli([sys.executable, "-c", "pass"], "", dict(os.environ), 30)
        self.assertEqual(returncode, 0)

    def test_a_stopped_call_is_not_reported_as_a_failed_answer(self):
        self.addCleanup(resume_clis)
        kill_running_clis()
        client = SubscriptionCliClient(5, ClaudeCli(), "haiku")
        client.executable = sys.executable

        with self.assertRaises(CliStoppedError):
            client.chat_json("prompt")


class CommandExitTest(unittest.TestCase):
    def test_a_subscription_limit_ends_the_command_with_a_message_not_a_traceback(self):
        with (
            patch("sys.argv", ["pauk", "dedup", "graph"]),
            patch("pauk.cli._dispatch", side_effect=SubscriptionLimitError("subscription limit reached: out")),
            patch("pauk.cli._interrupt_on_termination"),
            self.assertRaisesRegex(SystemExit, "after the limit resets"),
        ):
            cli.main()

    def test_a_signed_out_cli_ends_the_command_with_a_nonzero_exit(self):
        with (
            patch("sys.argv", ["pauk", "dedup", "graph"]),
            patch("pauk.cli._dispatch", side_effect=CliUnusableError("CLI is not signed in: Not logged in")),
            patch("pauk.cli._interrupt_on_termination"),
            self.assertRaisesRegex(SystemExit, "Fix the CLI"),
        ):
            cli.main()

    def test_termination_is_turned_into_an_interrupt_for_llm_commands_only(self):
        for command, expected in ((["dedup", "graph"], 1), (["cache", "export"], 0)):
            with (
                self.subTest(command=command),
                patch("sys.argv", ["pauk", *command]),
                patch("pauk.cli._dispatch"),
                patch("pauk.cli._interrupt_on_termination") as guard,
            ):
                cli.main()
                self.assertEqual(guard.call_count, expected)

    def test_the_handler_raises_the_interrupt_the_stages_clean_up_on(self):
        with patch("pauk.cli.signal.signal") as install:
            cli._interrupt_on_termination()

        handler = install.call_args.args[1]
        with self.assertRaises(KeyboardInterrupt):
            handler(15, None)


class LlmProviderTest(unittest.TestCase):
    def test_provider_picks_the_client_and_model(self):
        self.assertIsInstance(llm_client("openrouter", timeout=5, api_key="k", openrouter_model="m"), OpenRouterClient)
        models = {
            provider: llm_client(provider, timeout=5, api_key="", openrouter_model="m").model
            for provider in ("claude:sonnet", "codex:gpt-x")
        }

        self.assertEqual(
            models,
            {"claude:sonnet": "claude-cli/sonnet", "codex:gpt-x": "codex-cli/gpt-x"},
        )
        for provider in ("claud", "claude", "codex", "gemini:flash", "openrouter:some/model"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                llm_client(provider, timeout=5, api_key="", openrouter_model="m")

    def test_a_provider_typo_is_caught_before_any_stage_runs(self):
        with patch("pauk.sources.llm.shutil.which", return_value="/usr/bin/cli"):
            check_providers(Settings(llm_provider="claude", person_resolution_provider="codex:gpt-x"))

        with self.assertRaisesRegex(ValueError, "^person_resolution: unknown LLM provider 'claud'"):
            check_providers(Settings(person_resolution_provider="claud"))
        with self.assertRaisesRegex(ValueError, "^author_names: codex needs a model"):
            check_providers(Settings(author_names_provider="codex", codex_cli_model=""))

    def test_a_missing_cli_is_caught_before_any_stage_runs(self):
        with (
            patch("pauk.sources.llm.shutil.which", return_value=None),
            self.assertRaisesRegex(ValueError, "^link_relevance: claude CLI not found"),
        ):
            check_providers(Settings(llm_provider="claude"))

    def test_openrouter_needs_no_cli(self):
        config = Settings(
            llm_provider="openrouter",
            link_relevance_provider="",
            author_names_provider="",
            person_resolution_provider="",
        )
        with patch("pauk.sources.llm.shutil.which", return_value=None):
            check_providers(config)

    def test_a_stage_setting_wins_and_dedup_switches_only_by_its_own(self):
        config = Settings(llm_provider="claude", author_names_provider="openrouter", person_resolution_provider="")

        self.assertEqual(config.provider_for("author_names"), "openrouter")
        self.assertEqual(config.provider_for("link_relevance"), f"claude:{config.claude_cli_model}")
        self.assertEqual(config.provider_for("person_resolution"), "openrouter")

    def test_a_bare_cli_name_takes_its_model_from_the_settings(self):
        config = Settings(
            llm_provider="claude",
            author_names_provider="claude:opus",
            person_resolution_provider="codex",
            claude_cli_model="sonnet",
            codex_cli_model="gpt-x",
        )

        self.assertEqual(config.provider_for("link_relevance"), "claude:sonnet")
        self.assertEqual(config.provider_for("author_names"), "claude:opus")
        self.assertEqual(config.provider_for("person_resolution"), "codex:gpt-x")

    def test_codex_without_a_model_in_the_settings_fails_the_start_check(self):
        config = Settings(llm_provider="codex", codex_cli_model="")

        with self.assertRaisesRegex(ValueError, "^link_relevance: codex needs a model"):
            check_providers(config)


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
