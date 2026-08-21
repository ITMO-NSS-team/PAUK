from __future__ import annotations

import json
import logging
import re

import requests

from .base import HttpClient

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_RAW_TEXT_LIMIT = 64 * 1024

logger = logging.getLogger(__name__)


def _strip_code_fence(content: str) -> str:
    """response_format=json_object is meant to guarantee a bare JSON body,
    but not every provider honors it - seen in the wild: claude-haiku-4.5
    via the Bedrock route wraps its answer in a ```json ... ``` block."""
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
        self.last_response: dict | None = None
        self.last_error: str | None = None

    def chat_json(self, prompt: str) -> dict | None:
        """POST one user message, return the parsed JSON reply or None.

        None on: no API key, network error, non-200 status, empty content
        (reasoning models sometimes do this) or invalid JSON - the caller
        treats all of these as one failed classification, not a crash. Every
        failure is logged (warning) with the reason, so a batch run shows
        exactly what went wrong instead of a silent gap in results.
        """
        self.last_usage = None
        self.last_response = None
        self.last_error = None
        if not self.api_key:
            self.last_error = "OPENROUTER_API_KEY not set"
            logger.warning("OpenRouter: OPENROUTER_API_KEY not set, skipping request")
            return None
        try:
            response = self.session.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
                timeout=self.timeout,
                proxies=self.proxies,
            )
        except requests.RequestException as exc:
            self.last_error = type(exc).__name__
            logger.warning("OpenRouter: request to %s failed: %s", self.model, exc)
            return None

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
        self.last_usage = payload.get("usage")
        if "error" in payload:
            # Some upstream routes answer HTTP 200 with an error body instead
            # of a 4xx/5xx (seen in the wild: OpenRouter round-robins one
            # model across providers, and a provider that doesn't support
            # response_format=json_object replies this way) - raise_for_status()
            # above can't catch this, so it's checked explicitly.
            message = (payload["error"] or {}).get("message")
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
