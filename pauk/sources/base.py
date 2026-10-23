from __future__ import annotations

import logging
import math
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests

from pauk.redaction import redact_url

logger = logging.getLogger(__name__)

LONG_WAIT_SECONDS = 10.0

# Requests per second each API allows one client, a little under the published
# limit: OpenAlex 30/s with a key, ORCID 12/s (registered public API), Crossref
# 10/s in the polite pool. Shared by every client and thread in the process,
# so a stage can run requests in parallel without tripping 429s and 503s.
HOST_RATES = {
    "api.openalex.org": 20.0,
    "pub.orcid.org": 10.0,
    "api.crossref.org": 8.0,
}


class HostRateLimiter:
    """Spaces requests to one host evenly, across all threads of the process."""

    def __init__(self, rates: dict[str, float]) -> None:
        self.rates = dict(rates)
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        rate = self.rates.get(host)
        if not rate:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + 1.0 / rate
        if slot > now:
            time.sleep(slot - now)


RATE_LIMITER = HostRateLimiter(HOST_RATES)


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

    def get_bytes(
        self, url: str, *, retries: int = 3, timeout: int | None = None, deadline: float | None = None,
        max_wait: float | None = None,
    ) -> bytes:
        """Send a GET request and return the raw response body.

        `timeout` bounds each socket read, and a buffered read waits for a whole
        chunk, so a server trickling a byte at a time keeps one download alive
        for hours; `deadline` (seconds) bounds the whole body read by shutting
        the socket down when it passes. `max_wait` gives up instead of retrying
        when the server asks for a longer pause (a Retry-After of an hour, say).
        """
        if deadline is None:
            return self._request("GET", url, retries=retries, timeout=timeout, max_wait=max_wait).content
        response = self._request("GET", url, retries=retries, timeout=timeout, stream=True, max_wait=max_wait)
        # A check between chunks is not enough: one chunk can take hours to fill.
        expired = threading.Event()
        watchdog = threading.Timer(deadline, self._abort_read, (response, expired))
        watchdog.daemon = True
        watchdog.start()
        chunks = []
        try:
            for chunk in response.iter_content(64 * 1024):
                chunks.append(chunk)
        except Exception as exc:
            if not expired.is_set() and not isinstance(exc, (requests.RequestException, OSError)):
                raise
            cause = f"body not received in {deadline:.0f}s" if expired.is_set() else type(exc).__name__
            raise HttpRequestError("GET", url, cause_type=cause) from None
        finally:
            watchdog.cancel()
            response.close()
        if expired.is_set():
            raise HttpRequestError("GET", url, cause_type=f"body not received in {deadline:.0f}s")
        return b"".join(chunks)

    @staticmethod
    def _abort_read(response: requests.Response, expired: threading.Event) -> None:
        """Wake a read blocked on this response's socket; closing alone does not."""
        expired.set()
        connection = getattr(response.raw, "connection", None) or getattr(response.raw, "_connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            # http.client hands the socket to the response when the server
            # closes after it (Connection: close); it lives in the reader then.
            reader = getattr(getattr(response.raw, "_fp", None), "fp", None)
            sock = getattr(getattr(reader, "raw", None), "_sock", None)
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def get_text(self, url: str, *, retries: int = 3, timeout: int | None = None) -> str:
        """Send a GET request and return the decoded response text (charset auto-detected)."""
        return self._request("GET", url, retries=retries, timeout=timeout).text

    def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any | None = None,
        data: dict[str, Any] | None = None, retries: int = 3, timeout: int | None = None, stream: bool = False,
        max_wait: float | None = None,
    ) -> requests.Response:
        method = method.upper()
        safe_url = self._safe_request_url(method, url, params)
        request_kwargs: dict[str, Any] = {"timeout": timeout or self.timeout}
        if params is not None:
            request_kwargs["params"] = params
        if json is not None:
            request_kwargs["json"] = json
        if data is not None:
            request_kwargs["data"] = data
        if stream:
            request_kwargs["stream"] = True

        for attempt in range(retries + 1):
            RATE_LIMITER.acquire(url)
            try:
                response = self.session.request(method, url, **request_kwargs)
            except requests.RequestException as exc:
                if attempt == retries:
                    raise HttpRequestError(method, safe_url, cause_type=type(exc).__name__) from None
                self._wait(self._backoff_delay(attempt), type(exc).__name__, method, safe_url, attempt, retries)
                continue

            delay = self._retry_delay(response, attempt)
            if delay is not None and max_wait is not None and delay > max_wait:
                delay = None
            if delay is not None and attempt < retries:
                self._wait(delay, self._retry_reason(response), method, safe_url, attempt, retries)
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

    @staticmethod
    def _wait(delay: float, reason: str, method: str, safe_url: str, attempt: int, retries: int) -> None:
        """Sleep before a retry, logging any wait of LONG_WAIT_SECONDS or more."""
        if delay >= LONG_WAIT_SECONDS:
            resume_at = (datetime.now().astimezone() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            logger.warning(
                "waiting %.0fs until %s before retrying %s %s (%s, retry %d/%d)",
                delay, resume_at, method, safe_url, reason, attempt + 1, retries,
            )
        time.sleep(delay)

    def _retry_reason(self, response: requests.Response) -> str:
        return f"HTTP {response.status_code}"

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
