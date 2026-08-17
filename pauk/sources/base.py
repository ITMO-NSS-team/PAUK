from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from pauk.redaction import redact_url


class HttpRequestError(requests.RequestException):
    """HTTP failure whose public representation contains no request credentials."""

    def __init__(
        self,
        method: str,
        url: str,
        *,
        status_code: int | None = None,
        cause_type: str | None = None,
    ) -> None:
        self.method = method.upper()
        self.url = redact_url(url)
        self.status_code = status_code
        reason = f"HTTP {status_code}" if status_code is not None else (cause_type or "network error")
        super().__init__(f"{self.method} request to {self.url} failed: {reason}")


class HttpClient:
    """Synchronous HTTP client with retry support."""

    RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

    def __init__(self, timeout: int, headers: dict[str, str] | None = None) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(headers or {})

    def get_json(self, url: str, *, params: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
        """Send a GET request and return its decoded JSON object."""
        return self.request_json("GET", url, params=params, retries=retries)

    def request_json(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any | None = None, retries: int = 3
    ) -> dict[str, Any]:
        """Send an HTTP request and retry transient failures, returning decoded JSON."""
        return self._request(method, url, params=params, json=json, retries=retries).json()

    def get_bytes(self, url: str, *, retries: int = 3, timeout: int | None = None) -> bytes:
        """Send a GET request and return the raw response body."""
        return self._request("GET", url, retries=retries, timeout=timeout).content

    def get_text(self, url: str, *, retries: int = 3, timeout: int | None = None) -> str:
        """Send a GET request and return the decoded response text (charset auto-detected)."""
        return self._request("GET", url, retries=retries, timeout=timeout).text

    def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any | None = None,
        retries: int = 3, timeout: int | None = None,
    ) -> requests.Response:
        method = method.upper()
        safe_url = self._safe_request_url(method, url, params)
        request_kwargs: dict[str, Any] = {"timeout": timeout or self.timeout}
        if params is not None:
            request_kwargs["params"] = params
        if json is not None:
            request_kwargs["json"] = json

        for attempt in range(retries + 1):
            try:
                response = self.session.request(method, url, **request_kwargs)
            except requests.RequestException as exc:
                if attempt == retries:
                    raise HttpRequestError(method, safe_url, cause_type=type(exc).__name__) from None
                time.sleep(self._backoff_delay(attempt))
                continue

            delay = self._retry_delay(response, attempt)
            if delay is not None and attempt < retries:
                time.sleep(delay)
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError:
                response_url = response.url if isinstance(response.url, str) else safe_url
                raise HttpRequestError(
                    method,
                    response_url,
                    status_code=response.status_code,
                ) from None
            return response
        raise RuntimeError(f"unreachable retry state for {redact_url(url)}")

    @staticmethod
    def _safe_request_url(method: str, url: str, params: dict[str, Any] | None) -> str:
        """Build the diagnostic URL once, redacting it before any request can fail."""
        try:
            prepared = requests.Request(method, url, params=params).prepare()
            return redact_url(prepared.url or url)
        except requests.RequestException:
            return redact_url(url)

    def _retry_delay(self, response: requests.Response, attempt: int) -> float | None:
        """Return the response retry delay, or None if it must not be retried."""
        if response.status_code not in self.RETRYABLE_STATUS_CODES:
            return None
        retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
        return retry_after if retry_after is not None else self._backoff_delay(attempt)

    @staticmethod
    def _parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
        """Return the Retry-After delay for either seconds or an HTTP date."""
        if not value:
            return None
        raw = value.strip()
        try:
            delay = float(raw)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            current = now or datetime.now(UTC)
            return max(0.0, (retry_at - current).total_seconds())
        return delay if delay >= 0 and math.isfinite(delay) else None

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Return a capped exponential backoff for a zero-based attempt."""
        return float(min(60, 2**attempt))
