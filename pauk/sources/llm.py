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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import requests

from .base import HttpClient

if TYPE_CHECKING:
    from pauk.settings import Settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_RAW_TEXT_LIMIT = 64 * 1024
_TRANSIENT_RETRIES = 2
_JSON_ONLY = "Reply with exactly one JSON object and nothing else."
_CLAUDE_DROPPED_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)
_CODEX_DROPPED_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
_GEMINI_DROPPED_VARS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_VERTEX_BASE_URL",
    "GEMINI_CLI_USE_COMPUTE_ADC",
)
_LIMIT_MARKERS = (
    "usage limit",
    "limit reached",
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
)
_GEMINI_LIMIT_MARKERS = ("resource_exhausted", "quota")

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
        super().__init__(timeout, {"Authorization": f"Bearer {api_key}"})
        self.api_key = api_key
        self.model = model
        # Routes only this client's traffic through a forward proxy (e.g. an
        # internal tunnel that bypasses a geo-block) - other HttpClient
        # instances (OpenAlex, Crossref, ...) are untouched.
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        if self.proxies:
            self.session.proxies.update(self.proxies)
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

    def usage(self) -> float:
        """Dollars this API key has spent so far, OpenRouter's running total.

        Raises:
            HttpRequestError: the key is rejected or OpenRouter is unreachable.
        """
        return float(self.get_json(OPENROUTER_KEY_URL)["data"]["usage"])

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
                    self._wait(
                        self._backoff_delay(attempt), type(exc).__name__, "POST", OPENROUTER_URL,
                        attempt, _TRANSIENT_RETRIES,
                    )
                    continue
                self.last_error = type(exc).__name__
                logger.warning("OpenRouter: request to %s failed: %s", self.model, exc)
                return None

            delay = self._retry_delay(response, attempt)
            if delay is not None and attempt < _TRANSIENT_RETRIES:
                self._wait(delay, self._retry_reason(response), "POST", OPENROUTER_URL, attempt, _TRANSIENT_RETRIES)
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


def _limited(message: str) -> bool:
    return any(marker in message.lower() for marker in _LIMIT_MARKERS)


@dataclass(frozen=True)
class CliReply:
    """What one CLI call produced: the model's text, or why there is none."""

    text: str | None
    usage: dict | None
    error: str | None
    raw: Any


class ClaudeCli:
    """`claude -p` on a Claude subscription, without user or project settings and with thinking off."""

    name = "claude"
    executable = "claude"
    default_model = "haiku"
    dropped_vars = _CLAUDE_DROPPED_VARS
    extra_env = {"MAX_THINKING_TOKENS": "0"}

    def command(self, executable: str, model: str, workdir: Path) -> list[str]:
        return [
            executable,
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
            model,
            "--system-prompt-file",
            str(workdir / "system.txt"),
        ]

    def stdin(self, system: str, prompt: str, workdir: Path) -> str:
        """The system prompt goes through a file: on Windows the npm shim runs through cmd.exe, which mangles quotes."""
        (workdir / "system.txt").write_text(system, encoding="utf-8")
        return prompt

    def parse(self, returncode: int, stdout: str, stderr: str, workdir: Path) -> CliReply:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            raw = {
                "_returncode": returncode,
                "_raw_text": stdout[:_RAW_TEXT_LIMIT],
                "_stderr": stderr[:_RAW_TEXT_LIMIT],
            }
            return CliReply(None, None, f"exit {returncode}, output is not JSON", raw)
        if not isinstance(payload, dict):
            return CliReply(None, None, f"CLI output is {type(payload).__name__}, not an object", payload)
        usage = payload.get("usage") or {}
        usage = {
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
            limited = payload.get("api_error_status") == 429 or _limited(message)
            error = f"subscription limit reached: {message}" if limited else f"CLI error: {message}"
            return CliReply(None, usage, error, payload)
        return CliReply(result, usage, None, payload)


class CodexCli:
    """`codex exec` on a ChatGPT subscription, read-only and without user config.

    The system prompt goes in front of the message; the reply is read from --output-last-message and token
    usage from the --json event stream.
    """

    name = "codex"
    executable = "codex"
    default_model = ""
    dropped_vars = _CODEX_DROPPED_VARS
    extra_env = {"NO_COLOR": "1"}

    def command(self, executable: str, model: str, workdir: Path) -> list[str]:
        return [
            executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--json",
            "--cd",
            str(workdir),
            "--output-last-message",
            str(workdir / "reply.txt"),
            "--model",
            model,
            "-",
        ]

    def stdin(self, system: str, prompt: str, workdir: Path) -> str:
        return f"{system}\n\n{prompt}"

    def parse(self, returncode: int, stdout: str, stderr: str, workdir: Path) -> CliReply:
        events = []
        for line in stdout.splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
        counts = _last_token_counts(events)
        usage = (
            {"prompt_tokens": counts.get("input_tokens") or 0, "completion_tokens": counts.get("output_tokens") or 0}
            if counts
            else None
        )
        reply = workdir / "reply.txt"
        text = reply.read_text(encoding="utf-8", errors="replace") if reply.exists() else ""
        raw = {"_returncode": returncode, "events": events[-20:], "_stderr": stderr[:_RAW_TEXT_LIMIT]}
        failure = _event_error(events)
        if returncode != 0 or (failure and not text.strip()):
            message = (failure or stderr.strip() or f"exit {returncode}")[-500:]
            error = f"subscription limit reached: {message}" if _limited(message) else f"CLI error: {message}"
            return CliReply(None, usage, error, raw)
        return CliReply(text, usage, None, raw)


class GeminiCli:
    """`gemini -p` on a Google account login, with extensions off and the system prompt replaced.

    GEMINI_SYSTEM_MD replaces the built-in prompt with a file; a relative path resolves against the call's own
    temp directory. Tools that would need a confirmation are denied in headless mode. The message goes through
    stdin and the fixed -p text is appended to it.
    """

    name = "gemini"
    executable = "gemini"
    default_model = ""
    dropped_vars = _GEMINI_DROPPED_VARS
    extra_env = {"GEMINI_SYSTEM_MD": "system.md", "GOOGLE_GENAI_USE_GCA": "true", "NO_COLOR": "1"}

    def command(self, executable: str, model: str, workdir: Path) -> list[str]:
        return [
            executable,
            "--prompt",
            _JSON_ONLY,
            "--output-format",
            "json",
            "--approval-mode",
            "default",
            "--extensions",
            "none",
            "--model",
            model,
        ]

    def stdin(self, system: str, prompt: str, workdir: Path) -> str:
        (workdir / "system.md").write_text(system, encoding="utf-8")
        return prompt

    def parse(self, returncode: int, stdout: str, stderr: str, workdir: Path) -> CliReply:
        start = stdout.find("{")
        try:
            payload = json.loads(stdout[start:]) if start >= 0 else None
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            raw = {
                "_returncode": returncode,
                "_raw_text": stdout[:_RAW_TEXT_LIMIT],
                "_stderr": stderr[:_RAW_TEXT_LIMIT],
            }
            return CliReply(None, None, f"exit {returncode}, output is not JSON", raw)
        tokens = [
            model.get("tokens") or {}
            for model in ((payload.get("stats") or {}).get("models") or {}).values()
            if isinstance(model, dict)
        ]
        usage = (
            {
                "prompt_tokens": sum(count.get("prompt") or 0 for count in tokens),
                "completion_tokens": sum(
                    (count.get("candidates") or 0) + (count.get("thoughts") or 0) for count in tokens
                ),
            }
            if tokens
            else None
        )
        failure = payload.get("error")
        text = payload.get("response")
        if failure or returncode != 0 or not isinstance(text, str):
            failure = failure if isinstance(failure, dict) else {}
            message = str(failure.get("message") or stderr.strip() or f"exit {returncode}")[-500:]
            limited = (
                failure.get("type") in ("TerminalQuotaError", "RetryableQuotaError")
                or failure.get("code") == 429
                or _limited(message)
                or any(marker in message.lower() for marker in _GEMINI_LIMIT_MARKERS)
            )
            error = f"subscription limit reached: {message}" if limited else f"CLI error: {message}"
            return CliReply(None, usage, error, payload)
        return CliReply(text, usage, None, payload)


def _last_token_counts(events: list[dict]) -> dict:
    """The last object in the event stream that carries input_tokens."""
    found: dict = {}

    def walk(value: Any) -> None:
        nonlocal found
        if isinstance(value, dict):
            if "input_tokens" in value:
                found = value
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(events)
    return found


def _event_error(events: list[dict]) -> str:
    for event in reversed(events):
        kind = str(event.get("type", ""))
        if "error" in kind or "fail" in kind:
            message = event.get("message") or event.get("error")
            return message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
    return ""


class SubscriptionCliClient:
    """A model behind a subscription CLI (`claude -p`, `codex exec`, `gemini -p`), with OpenRouterClient's chat_json.

    Variables that would bill an API instead of the subscription, or cap the reply, are dropped from the
    child's environment. Each call runs in its own temp directory.
    """

    def __init__(self, timeout: int, backend: ClaudeCli | CodexCli | GeminiCli, model: str) -> None:
        self.timeout = timeout
        self.backend = backend
        self.cli_model = model
        self.model = f"{backend.name}-cli/{model}"
        self.executable = shutil.which(backend.executable)
        self.last_usage: dict | None = None
        self.last_response: Any | None = None
        self.last_error: str | None = None

    def unavailable_reason(self) -> str | None:
        """Why no call can be made, or None."""
        return None if self.executable else f"{self.backend.executable} CLI not found"

    def _env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key not in self.backend.dropped_vars}
        env.update(self.backend.extra_env)
        return env

    def _fail(self, error: str) -> None:
        self.last_error = error
        logger.warning("%s CLI: %s: %s", self.backend.name, self.cli_model, error)

    def chat_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict | None:
        """Run one CLI call, return the parsed JSON reply or None, as OpenRouterClient does.

        max_tokens is ignored: over a cap the CLI continues the answer and returns only its tail.
        """
        del max_tokens
        self.last_usage = None
        self.last_response = None
        self.last_error = None
        if not self.executable:
            self._fail(f"{self.backend.executable} CLI not found")
            return None
        system = f"{system_prompt}\n\n{_JSON_ONLY}" if system_prompt else _JSON_ONLY
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workdir = Path(directory)
            try:
                stdin = self.backend.stdin(system, prompt, workdir)
                command = self.backend.command(self.executable, self.cli_model, workdir)
                returncode, stdout, stderr = _run_cli(command, stdin, self._env(), self.timeout, workdir)
            except subprocess.TimeoutExpired:
                self._fail(f"timed out after {self.timeout}s")
                return None
            except OSError as exc:
                self._fail(type(exc).__name__)
                return None
            reply = self.backend.parse(returncode, stdout, stderr, workdir)
        self.last_response = reply.raw
        self.last_usage = reply.usage
        if reply.error:
            self._fail(reply.error)
            return None
        if not reply.text or not reply.text.strip():
            self._fail("empty model response")
            return None
        try:
            parsed = json.loads(_strip_code_fence(reply.text))
        except json.JSONDecodeError as exc:
            self._fail(f"could not parse model response: {type(exc).__name__}")
            return None
        if not isinstance(parsed, dict):
            self._fail(f"model response is {type(parsed).__name__}, not an object")
            return None
        return parsed


_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the CLI with its children: on Windows the npm shim starts node under cmd.exe, which outlives cmd.exe."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def kill_running_clis() -> None:
    """Kill every CLI call still running, with its children.

    Each CLI runs in its own process group so that a timeout can kill its children, which also keeps Ctrl-C from
    reaching it: a stage interrupted while worker threads wait on CLIs has to kill them itself.
    """
    with _running_lock:
        processes = list(_running)
    for process in processes:
        _kill_tree(process)


def _run_cli(
    command: list[str], prompt: str, env: dict[str, str], timeout: int, cwd: Path | None = None
) -> tuple[int, str, str]:
    """Run the CLI; on timeout or interruption kill it with its children."""
    windows = sys.platform == "win32"
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd or tempfile.gettempdir(),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        start_new_session=not windows,
    )
    with _running_lock:
        _running.add(process)
    try:
        stdout, stderr = process.communicate(prompt.encode("utf-8"), timeout=timeout)
    except BaseException:
        _kill_tree(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=10)
        raise
    finally:
        with _running_lock:
            _running.discard(process)
    return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


_CLIS = {"claude": ClaudeCli, "codex": CodexCli, "gemini": GeminiCli}
LLM_STAGES = ("link_relevance", "author_names", "person_resolution")


def parse_provider(provider: str) -> tuple[str, str]:
    """The provider name and CLI model of a setting: "openrouter", "claude[:<model>]", "<codex|gemini>:<model>".

    Raises:
        ValueError: the name is unknown, or the CLI has no default model and none is given.

    Example:
        >>> parse_provider(""), parse_provider("claude"), parse_provider("codex:gpt-5")
        (('openrouter', ''), ('claude', 'haiku'), ('codex', 'gpt-5'))
    """
    name, _, model = provider.strip().partition(":")
    if name in ("", "openrouter"):
        return "openrouter", ""
    if name not in _CLIS:
        raise ValueError(
            f"unknown LLM provider {provider!r}, expected openrouter, {', '.join(_CLIS)} or <cli>:<model>"
        )
    model = model or _CLIS[name].default_model
    if not model:
        raise ValueError(f"{name} needs a model, e.g. {name}:<model>: its default changes with the CLI version")
    return name, model


def check_providers(config: Settings) -> None:
    """Parse every LLM stage's provider setting, so that a typo stops a run at its start, not at the stage hours in.

    Raises:
        ValueError: a stage's provider setting does not parse (see parse_provider).
    """
    for stage in LLM_STAGES:
        try:
            parse_provider(config.provider_for(stage))
        except ValueError as exc:
            raise ValueError(f"{stage}: {exc}") from None


def llm_client(provider: str, *, timeout: int, api_key: str, openrouter_model: str, proxy_url: str = "") -> LlmClient:
    """A client for a provider setting (see parse_provider). A CLI call gets at least 120 s."""
    name, model = parse_provider(provider)
    if name == "openrouter":
        return OpenRouterClient(timeout, api_key, openrouter_model, proxy_url)
    return SubscriptionCliClient(max(timeout, 120), _CLIS[name](), model)
