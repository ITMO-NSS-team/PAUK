from __future__ import annotations

import json
import logging
import re

import requests

from .base import HttpClient

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

logger = logging.getLogger(__name__)


def _strip_code_fence(content: str) -> str:
    """response_format=json_object is meant to guarantee a bare JSON body,
    but not every provider honors it - seen in the wild: claude-haiku-4.5
    via the Bedrock route wraps its answer in a ```json ... ``` block."""
    match = _CODE_FENCE.match(content.strip())
    return match.group(1) if match else content


class OpenRouterClient(HttpClient):
    """Minimal OpenRouter chat-completions client, JSON-object mode only."""

    def __init__(self, timeout: int, api_key: str, model: str) -> None:
        super().__init__(timeout)
        self.api_key = api_key
        self.model = model
        # Token usage from the most recent chat_json() call (or None if it
        # failed before OpenRouter replied) - read by callers that need to
        # track cost, e.g. the model-comparison script.
        self.last_usage: dict | None = None

    def chat_json(self, prompt: str) -> dict | None:
        """POST one user message, return the parsed JSON reply or None.

        None on: no API key, network error, non-200 status, empty content
        (reasoning models sometimes do this) or invalid JSON - the caller
        treats all of these as one failed classification, not a crash. Every
        failure is logged (warning) with the reason, so a batch run shows
        exactly what went wrong instead of a silent gap in results.
        """
        self.last_usage = None
        if not self.api_key:
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
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("OpenRouter: request to %s failed: %s", self.model, exc)
            return None
        payload = response.json()
        self.last_usage = payload.get("usage")
        if "error" in payload:
            # Some upstream routes answer HTTP 200 with an error body instead
            # of a 4xx/5xx (seen in the wild: OpenRouter round-robins one
            # model across providers, and a provider that doesn't support
            # response_format=json_object replies this way) - raise_for_status()
            # above can't catch this, so it's checked explicitly.
            logger.warning("OpenRouter: %s returned an error body: %s",
                            self.model, (payload["error"] or {}).get("message"))
            return None
        try:
            content = payload["choices"][0]["message"].get("content")
            if not content:
                logger.warning("OpenRouter: %s returned an empty response body", self.model)
                return None
            parsed = json.loads(_strip_code_fence(content))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("OpenRouter: could not parse %s's response: %s", self.model, exc)
            return None
        if not isinstance(parsed, dict):
            # response_format=json_object is meant to guarantee an object,
            # but not every model actually honors it (seen in the wild:
            # gpt-oss-20b wrapping the verdict in a list).
            logger.warning("OpenRouter: %s replied with %s, not a JSON object",
                            self.model, type(parsed).__name__)
            return None
        return parsed
