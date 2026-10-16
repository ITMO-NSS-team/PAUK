from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Protocol

import requests

from .base import HttpClient

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_RAW_TEXT_LIMIT = 64 * 1024
_TRANSIENT_RETRIES = 2
_JSON_ONLY = "Reply with exactly one JSON object and nothing else."
_DROPPED_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)
_LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "rate_limit")

logger = logging.getLogger(__name__)


def _strip_code_fence(content: str) -> str:
    """Strip a markdown code fence from a reply.

    response_format=json_object should guarantee a bare JSON body, but some
    providers ignore it (e.g. claude-haiku-4.5 via Bedrock fences its answer).
    """
    match = _CODE_FENCE.match(content.strip())
    return match.group(1) if match else content


class OpenRouterClient(HttpClient):
    """Minimal OpenRouter chat-completions client, JSON-object mode only."""

    def __init__(self, timeout: int, api_key: str, model: str, proxy_url: str = "") -> None:
        super().__init__(timeout)
        self.api_key = api_key
        self.model = model
        # Routes only this client's traffic through a forward proxy (e.g. an
        # internal tunnel that bypasses a geo-block) - other HttpClient
        # instances (OpenAlex, Crossref, ...) are untouched.
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        # Token usage from the most recent chat_json() call (or None if it
        # failed before OpenRouter replied) - read by callers that need to
        # track cost, e.g. the model-comparison script.
        self.last_usage: dict | None = None
        # The full parsed OpenRouter response body from the most recent
        # chat_json() call (or None if it failed before OpenRouter replied) -
        # read by callers that log LLM calls in full (see LlmLogStore).
        self.last_response: Any | None = None
        self.last_error: str | None = None

    def unavailable_reason(self) -> str | None:
        """Why no call can be made, or None."""
        return None if self.api_key else "OPENROUTER_API_KEY not set"

    def chat_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict | None:
        """POST one user message, return the parsed JSON reply or None.

        None on: no API key, exhausted network/transient-HTTP retries,
        non-retryable status, empty content (reasoning models sometimes do
        this) or invalid JSON - the caller treats all of these as one failed
        classification, not a crash. Every final failure is logged (warning)
        with the reason, so a batch run shows exactly what went wrong instead
        of a silent gap in results.
        """
        self.last_usage = None
        self.last_response = None
        self.last_error = None
        if not self.api_key:
            self.last_error = "OPENROUTER_API_KEY not set"
            logger.warning("OpenRouter: OPENROUTER_API_KEY not set, skipping request")
            return None
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        response = None
        for attempt in range(_TRANSIENT_RETRIES + 1):
            try:
                response = self.session.post(
                    OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                    timeout=self.timeout,
                    proxies=self.proxies,
                )
            except requests.RequestException as exc:
                if attempt < _TRANSIENT_RETRIES:
                    time.sleep(self._backoff_delay(attempt))
                    continue
                self.last_error = type(exc).__name__
                logger.warning("OpenRouter: request to %s failed: %s", self.model, exc)
                return None

            delay = self._retry_delay(response, attempt)
            if delay is not None and attempt < _TRANSIENT_RETRIES:
                time.sleep(delay)
                continue
            break

        if response is None:  # pragma: no cover - the loop always returns or assigns it
            raise RuntimeError("OpenRouter request loop finished without a response")

        try:
            payload = response.json()
        except ValueError:
            logger.warning("OpenRouter: %s returned a non-JSON response body", self.model)
            payload = {
                "_http_status": response.status_code,
                "_content_type": response.headers.get("Content-Type"),
                "_raw_text": (response.text or "")[:_RAW_TEXT_LIMIT],
            }
        self.last_response = payload
        try:
            response.raise_for_status()
        except requests.HTTPError:
            self.last_error = f"HTTP {response.status_code}"
            logger.warning("OpenRouter: request to %s failed with HTTP %s", self.model, response.status_code)
            return None
        if not isinstance(payload, dict):
            self.last_error = f"OpenRouter response is {type(payload).__name__}, not an object"
            logger.warning(
                "OpenRouter: %s returned a %s response envelope, not an object",
                self.model,
                type(payload).__name__,
            )
            return None
        self.last_usage = payload.get("usage")
        if "error" in payload:
            # Some upstream routes answer HTTP 200 with an error body instead
            # of a 4xx/5xx (seen in the wild: OpenRouter round-robins one
            # model across providers, and a provider that doesn't support
            # response_format=json_object replies this way) - raise_for_status()
            # above can't catch this, so it's checked explicitly.
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error or "")
            self.last_error = f"OpenRouter error: {message or 'unknown error'}"
            logger.warning("OpenRouter: %s returned an error body: %s", self.model, message)
            return None
        try:
            content = payload["choices"][0]["message"].get("content")
            if not content:
                self.last_error = "empty model response"
                logger.warning("OpenRouter: %s returned an empty response body", self.model)
                return None
            parsed = json.loads(_strip_code_fence(content))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            self.last_error = f"could not parse model response: {type(exc).__name__}"
            logger.warning("OpenRouter: could not parse %s's response: %s", self.model, exc)
            return None
        if not isinstance(parsed, dict):
            # response_format=json_object is meant to guarantee an object,
            # but not every model actually honors it (seen in the wild:
            # gpt-oss-20b wrapping the verdict in a list).
            self.last_error = f"model response is {type(parsed).__name__}, not an object"
            logger.warning("OpenRouter: %s replied with %s, not a JSON object", self.model, type(parsed).__name__)
            return None
        return parsed


class LlmClient(Protocol):
    """What the LLM stages need from a client: one JSON reply per call."""

    model: str
    last_usage: dict | None
    last_response: Any | None
    last_error: str | None

    def unavailable_reason(self) -> str | None: ...

    def chat_json(
        self, prompt: str, *, system_prompt: str | None = None, max_tokens: int | None = None
    ) -> dict | None: ...


class ClaudeCliClient:
    """Claude through the local `claude -p` CLI, billed to its logged-in Claude subscription.

    Variables that would bill the API or another provider, or cap the reply, are dropped from the
    child's environment, and no user or project settings are loaded. The system prompt goes through
    a file: on Windows the npm shim runs through cmd.exe, which mangles quotes.
    """

    def __init__(self, timeout: int, model: str = "haiku", executable: str = "claude") -> None:
        self.timeout = timeout
        self.cli_model = model
        self.model = f"claude-cli/{model}"
        self.executable = shutil.which(executable)
        self.last_usage: dict | None = None
        self.last_response: Any | None = None
        self.last_error: str | None = None

    def unavailable_reason(self) -> str | None:
        """Why no call can be made, or None."""
        return None if self.executable else "claude CLI not found"

    def _command(self, system_file: str) -> list[str]:
        return [
            self.executable or "claude",
            "-p",
            "--safe-mode",
            "--setting-sources",
            "",
            "--tools",
            "",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--model",
            self.cli_model,
            "--system-prompt-file",
            system_file,
        ]

    @staticmethod
    def _env() -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key not in _DROPPED_VARS}
        env["MAX_THINKING_TOKENS"] = "0"
        return env

    def _fail(self, error: str) -> None:
        self.last_error = error
        logger.warning("Claude CLI: %s: %s", self.cli_model, error)

    def chat_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict | None:
        """Run one `claude -p` call, return the parsed JSON reply or None, as OpenRouterClient does.

        max_tokens is ignored: over the cap the CLI continues the answer and returns only its tail.
        """
        del max_tokens
        self.last_usage = None
        self.last_response = None
        self.last_error = None
        if not self.executable:
            self._fail("claude CLI not found")
            return None
        system = f"{system_prompt}\n\n{_JSON_ONLY}" if system_prompt else _JSON_ONLY
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(system)
        try:
            returncode, stdout, stderr = _run_cli(self._command(handle.name), prompt, self._env(), self.timeout)
        except subprocess.TimeoutExpired:
            self._fail(f"timed out after {self.timeout}s")
            return None
        except OSError as exc:
            self._fail(type(exc).__name__)
            return None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            self.last_response = {
                "_returncode": returncode,
                "_raw_text": stdout[:_RAW_TEXT_LIMIT],
                "_stderr": stderr[:_RAW_TEXT_LIMIT],
            }
            self._fail(f"exit {returncode}, output is not JSON")
            return None
        self.last_response = payload
        if not isinstance(payload, dict):
            self._fail(f"CLI output is {type(payload).__name__}, not an object")
            return None
        usage = payload.get("usage") or {}
        self.last_usage = {
            "prompt_tokens": sum(
                usage.get(key) or 0
                for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            ),
            "completion_tokens": usage.get("output_tokens") or 0,
            "cost": payload.get("total_cost_usd"),
        }
        result = payload.get("result")
        if payload.get("is_error") or not isinstance(result, str):
            message = str(result or payload.get("subtype") or "error")
            limited = payload.get("api_error_status") == 429 or any(
                marker in message.lower() for marker in _LIMIT_MARKERS
            )
            self._fail(f"subscription limit reached: {message}" if limited else f"CLI error: {message}")
            return None
        if not result:
            self._fail("empty model response")
            return None
        try:
            parsed = json.loads(_strip_code_fence(result))
        except json.JSONDecodeError as exc:
            self._fail(f"could not parse model response: {type(exc).__name__}")
            return None
        if not isinstance(parsed, dict):
            self._fail(f"model response is {type(parsed).__name__}, not an object")
            return None
        return parsed


def _run_cli(command: list[str], prompt: str, env: dict[str, str], timeout: int) -> tuple[int, str, str]:
    """Run the CLI in the temp directory; on timeout kill it with its children.

    On Windows the npm shim starts node under cmd.exe, and killing cmd.exe alone leaves node running.
    """
    windows = sys.platform == "win32"
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tempfile.gettempdir(),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        start_new_session=not windows,
    )
    try:
        stdout, stderr = process.communicate(prompt.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        if windows:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def llm_client(provider: str, *, timeout: int, api_key: str, openrouter_model: str, proxy_url: str = "") -> LlmClient:
    """A client for a provider setting: "openrouter", "claude" (haiku) or "claude:<model>".

    A CLI call gets at least 120 s.
    """
    name, _, cli_model = provider.strip().partition(":")
    if name in ("", "openrouter"):
        return OpenRouterClient(timeout, api_key, openrouter_model, proxy_url)
    if name == "claude":
        return ClaudeCliClient(max(timeout, 120), cli_model or "haiku")
    raise ValueError(f"unknown LLM provider {provider!r}, expected openrouter, claude or claude:<model>")
