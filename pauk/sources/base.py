from __future__ import annotations

import logging
import math
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError as UrllibError
from urllib3.util import Timeout

from pauk.redaction import redact_url
from pauk.settings import (
    ARXIV_RPS,
    CROSSREF_PUBLIC_CONCURRENCY,
    CROSSREF_PUBLIC_RPS,
    OPENALEX_PUBLIC_RPS,
    ORCID_RPS,
)

from .http_stream import watch_headers
from .rate_limit import LONG_WAIT_SECONDS, HostPaused, HostRateLimiter, RateLimit, RequestDeferred

logger = logging.getLogger(__name__)

PUBLIC_LIMITS = {
    "api.openalex.org": RateLimit(OPENALEX_PUBLIC_RPS),
    "pub.orcid.org": RateLimit(ORCID_RPS),
    "api.crossref.org": RateLimit(CROSSREF_PUBLIC_RPS, CROSSREF_PUBLIC_CONCURRENCY),
    "arxiv.org": RateLimit(ARXIV_RPS, 1),
    "www.arxiv.org": RateLimit(ARXIV_RPS, 1),
    "export.arxiv.org": RateLimit(ARXIV_RPS, 1),
}
RATE_LIMITER = HostRateLimiter()
DOWNLOAD_CHUNK_BYTES = 64 * 1024


class _DownloadDeadline(requests.Timeout):
    """Distinguish the attempt deadline from a socket timeout."""


@dataclass
class DownloadState:
    """Resume a deferred download without repeating a completed redirect or retry."""

    attempt: int = 0
    deadline: float | None = None
    prepared: requests.PreparedRequest | None = None
    history: list[requests.Response] = field(default_factory=list)


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
        if status_code is not None and cause_type is not None:
            reason += f" ({cause_type})"
        super().__init__(f"{self.method} request to {self.url} failed: {reason}")


class HttpClient:
    """Synchronous HTTP client with retry support."""

    RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self, timeout: int, headers: dict[str, str] | None = None, *,
        rate_limits: dict[str, RateLimit] | None = None,
        rate_limiter: HostRateLimiter | None = None,
        default_rate_limit: RateLimit | None = None,
        default_rate_limiter: HostRateLimiter | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(headers or {})
        self.rate_limits = PUBLIC_LIMITS | (rate_limits or {})
        self.rate_limiter = rate_limiter if rate_limiter is not None else RATE_LIMITER
        self.default_rate_limit = default_rate_limit
        self.default_rate_limiter = default_rate_limiter if default_rate_limiter is not None else self.rate_limiter
        self._cancel = cancel
        self._cancelled = threading.Event()
        self._active_reads: set[Callable[[], None]] = set()
        self._read_lock = threading.Lock()

    def cancel(self) -> None:
        """Stop retries and release active streaming reads."""
        self._cancelled.set()
        with self._read_lock:
            for abort in self._active_reads:
                abort()

    def close(self) -> None:
        self.cancel()
        self.session.close()

    def get_json(self, url: str, *, params: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
        """Send a GET request and return its decoded JSON object."""
        return self.request_json("GET", url, params=params, retries=retries)

    def request_json(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any | None = None, retries: int = 3
    ) -> dict[str, Any]:
        """Send an HTTP request and retry transient failures, returning decoded JSON."""
        return self._request(method, url, params=params, json=json, retries=retries).json()

    def get_bytes(self, url: str, *, retries: int = 3, timeout: float | tuple[float, float] | None = None,
                  deadline: float | None = None, max_wait: float | None = None,
                  state: DownloadState | None = None) -> bytes:
        """Send a GET request and return the raw response body."""
        return self._bytes_response(url, retries=retries, timeout=timeout, deadline=deadline,
                                    max_wait=max_wait, state=state).content

    def _bytes_response(self, url: str, *, retries: int, timeout: float | tuple[float, float] | None,
                        deadline: float | None, max_wait: float | None,
                        state: DownloadState | None) -> requests.Response:
        if deadline is None and max_wait is None and state is None:
            return self._request("GET", url, retries=retries, timeout=timeout)
        if deadline is not None and (isinstance(deadline, bool) or not math.isfinite(deadline) or deadline <= 0):
            raise ValueError("deadline must be positive and finite")
        if max_wait is not None and (isinstance(max_wait, bool) or not math.isfinite(max_wait) or max_wait < 0):
            raise ValueError("max_wait must be nonnegative and finite")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a nonnegative integer")
        return self._request("GET", url, retries=retries, timeout=timeout, deadline=deadline,
                             max_wait=max_wait, consume=self._read_body, state=state)

    def _read_body(self, response: requests.Response) -> bytes:
        chunks = []
        for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
            if self._cancelled.is_set():
                raise CancelledError
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _abort_read(response: requests.Response) -> None:
        """Interrupt a blocked read; closing the response alone can wait for it."""
        connection = getattr(response.raw, "connection", None) or getattr(response.raw, "_connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            # Connection: close responses keep the socket on the reader.
            reader = getattr(getattr(response.raw, "_fp", None), "fp", None)
            sock = getattr(getattr(reader, "raw", None), "_sock", None)
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    @contextmanager
    def _watch_read(self, interrupt: Callable[[], None], end: float | None) -> Iterator[None]:
        def abort():
            with suppress(OSError):
                interrupt()

        expired = threading.Event()

        def expire():
            expired.set()
            abort()

        timer = threading.Timer(max(0, end - time.monotonic()), expire) if end is not None else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        with self._read_lock:
            self._active_reads.add(abort)
        try:
            if self._cancelled.is_set():
                raise CancelledError
            if end is not None and time.monotonic() >= end:
                raise _DownloadDeadline
            yield
            if self._cancelled.is_set():
                raise CancelledError
            if expired.is_set() or (end is not None and time.monotonic() >= end):
                raise _DownloadDeadline
        except (requests.RequestException, UrllibError, OSError):
            if self._cancelled.is_set():
                raise CancelledError from None
            if expired.is_set() or (end is not None and time.monotonic() >= end):
                raise _DownloadDeadline from None
            raise
        finally:
            if timer is not None:
                # The watchdog must finish before the socket can be reused.
                timer.cancel()
                timer.join()
            with self._read_lock:
                self._active_reads.discard(abort)

    @contextmanager
    def _stream_response(self, response: requests.Response, end: float | None) -> Iterator[None]:
        try:
            with self._watch_read(lambda: self._abort_read(response), end):
                yield
        finally:
            response.close()

    @contextmanager
    def _watch_headers(self, url: str, end: float | None) -> Iterator[None]:
        with watch_headers(self.session, url, lambda sock: self._watch_read(lambda: sock.shutdown(socket.SHUT_RDWR), end)):
            try:
                yield
            except (requests.RequestException, UrllibError, OSError):
                # urllib3 can wrap a header deadline as a transport error.
                if end is not None and time.monotonic() >= end:
                    raise _DownloadDeadline from None
                raise

    def get_bytes_with_url(self, url: str, *, retries: int = 3, timeout: float | tuple[float, float] | None = None,
                           deadline: float | None = None, max_wait: float | None = None,
                           state: DownloadState | None = None) -> tuple[bytes, str]:
        """Return the response bytes and final URL after following redirects."""
        response = self._bytes_response(url, retries=retries, timeout=timeout, deadline=deadline,
                                        max_wait=max_wait, state=state)
        return response.content, response.url

    def get_text(self, url: str, *, retries: int = 3, timeout: int | None = None) -> str:
        """Send a GET request and return the decoded response text (charset auto-detected)."""
        return self._request("GET", url, retries=retries, timeout=timeout).text

    def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any | None = None,
        data: dict[str, Any] | None = None, allow_redirects: bool = True,
        retries: int = 3, timeout: float | tuple[float, float] | None = None,
        deadline: float | None = None, max_wait: float | None = None,
        consume: Callable[[requests.Response], bytes] | None = None,
        state: DownloadState | None = None,
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
        if not allow_redirects:
            request_kwargs["allow_redirects"] = False

        for attempt in range(state.attempt if state is not None else 0, retries + 1):
            if self._cancelled.is_set():
                raise CancelledError
            try:
                if consume is None:
                    response = self._send(method, url, request_kwargs, attempt)
                else:
                    response = self._send(method, url, request_kwargs, attempt, consume=consume,
                                          deadline=deadline, max_wait=max_wait, state=state)
            except HostPaused as exc:
                raise HttpRequestError(method, safe_url, cause_type=str(exc)) from None
            except (requests.RequestException, UrllibError, OSError) as exc:
                if self._cancelled.is_set():
                    raise CancelledError from None
                delay = self._backoff_delay(attempt)
                if attempt == retries or (max_wait is not None and delay > max_wait):
                    cause = "download deadline exceeded" if isinstance(exc, (_DownloadDeadline, TimeoutError)) else type(exc).__name__
                    raise HttpRequestError(method, safe_url, cause_type=cause) from None
                reason = type(exc).__name__
            else:
                delay = self._retry_delay(response, attempt)
                if delay is not None and max_wait is not None and delay > max_wait:
                    raise HttpRequestError(method, safe_url, status_code=response.status_code,
                                           cause_type=f"retry delayed for {delay:.0f}s") from None
                if delay is None or attempt == retries:
                    try:
                        response.raise_for_status()
                    except requests.HTTPError:
                        response_url = response.url if isinstance(response.url, str) else safe_url
                        raise HttpRequestError(method, response_url, status_code=response.status_code) from None
                    return response
                reason = self._retry_reason(response)
            self._wait(delay, reason, method, safe_url, attempt, retries,
                       cancel=self._cancelled if consume is not None else self._cancel, state=state)
        raise RuntimeError(f"unreachable retry state for {redact_url(url)}")

    def _send(self, method: str, url: str, kwargs: dict, attempt: int, *,
              consume: Callable[[requests.Response], bytes] | None = None,
              deadline: float | None = None, max_wait: float | None = None,
              state: DownloadState | None = None) -> requests.Response:
        """Admit each redirect separately, using Requests' prepared next request."""
        prepared = state.prepared if state is not None else None
        history = state.history if state is not None else []
        end = state.deadline if state is not None else None
        while True:
            if self._cancel is not None and self._cancel.is_set():
                raise CancelledError
            current_url = prepared.url if prepared is not None else url
            try:
                host = urlsplit(current_url).hostname or ""
            except ValueError:
                host = ""
            limit = self.rate_limits.get(host)
            scoped_limit = None
            if self.default_rate_limit is not None:
                if self.default_rate_limiter is self.rate_limiter:
                    limit = limit.stricter(self.default_rate_limit) if limit else self.default_rate_limit
                else:
                    scoped_limit = self.default_rate_limit
                    limit = limit or RateLimit()
            if prepared is not None and limit is not None:
                previous = urlsplit(history[-1].url)
                current = urlsplit(current_url)
                if host in PUBLIC_LIMITS and (previous.scheme, previous.netloc) != (current.scheme, current.netloc):
                    # Requests may have stripped credentials at the origin boundary.
                    limit = limit.stricter(PUBLIC_LIMITS[host])
            if prepared is None and limit is None and consume is None:
                return self.session.request(method, url, **kwargs)
            admission = ({"cancel": self._cancelled, "deadline": end, "max_wait": max_wait, "defer": state is not None}
                         if consume is not None else {"cancel": self._cancel})
            scoped = (self.default_rate_limiter, scoped_limit) if scoped_limit is not None else None
            with ExitStack() as cleanup:
                cleanup.enter_context(self.rate_limiter.slot(host, limit, scoped=scoped, **admission))
                request_kwargs = dict(kwargs)
                if consume is not None:
                    if self._cancelled.is_set():
                        raise CancelledError
                    if end is None and deadline is not None:
                        end = time.monotonic() + deadline
                        if state is not None:
                            state.deadline = end
                    if end is not None:
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            raise _DownloadDeadline
                        timeout = kwargs["timeout"]
                        connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
                        request_kwargs["timeout"] = Timeout(total=remaining, connect=min(connect, remaining),
                                                            read=min(read, remaining))

                    # Requests may read redirect bodies before send() returns.
                    def registered(response, cleanup=cleanup, end=end, **_kwargs):
                        cleanup.enter_context(self._stream_response(response, end))
                        return response

                    request_kwargs.update(stream=True, hooks={"response": registered})
                    cleanup.enter_context(self._watch_headers(current_url, end))
                if prepared is None:
                    response = self.session.request(method, url, **(request_kwargs | {"allow_redirects": False}))
                else:
                    send_kwargs = self.session.merge_environment_settings(prepared.url, {}, None, None, None)
                    if consume is not None:
                        prepared.hooks = request_kwargs["hooks"]
                        send_kwargs["stream"] = True
                    response = self.session.send(
                        prepared, timeout=request_kwargs["timeout"], allow_redirects=False, **send_kwargs,
                    )
                if limit is not None:
                    self._observe_limit(host, response, attempt)
                if consume is not None and not response.is_redirect and response.status_code < 400:
                    response._content = consume(response)
                    response._content_consumed = True
            following = (getattr(response, "next", None)
                         if kwargs.get("allow_redirects", True) and getattr(response, "is_redirect", False) else None)
            if following is None:
                if history:
                    response.history = history
                return response
            history.append(response)
            if len(history) > self.session.max_redirects:
                response.close()
                raise requests.TooManyRedirects
            prepared = following
            if state is not None:
                state.prepared = prepared

    def _observe_limit(self, host: str, response: requests.Response, attempt: int) -> None:
        count = self._positive_header(response, "X-Rate-Limit-Limit")
        interval = re.fullmatch(r"([\d.]+)s", response.headers.get("X-Rate-Limit-Interval", ""))
        seconds = self._positive_number(interval[1]) if interval is not None else None
        rate = count / seconds if count is not None and seconds is not None else None
        concurrency = self._positive_header(response, "X-Concurrency-Limit")
        self.rate_limiter.restrict(
            host, requests_per_second=rate if rate is not None and math.isfinite(rate) and rate > 0 else None,
            max_concurrent=int(concurrency) if concurrency is not None and concurrency.is_integer() else None,
        )
        if response.status_code in (429, 503) or (
            response.status_code in self.RETRYABLE_STATUS_CODES and response.headers.get("Retry-After")
        ):
            delay = self._retry_delay(response, attempt)
            if delay is not None:
                self.rate_limiter.pause(host, delay)

    @staticmethod
    def _positive_number(value: str | None) -> float | None:
        try:
            number = float(value) if value is not None else 0
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @classmethod
    def _positive_header(cls, response: requests.Response, name: str) -> float | None:
        return cls._positive_number(response.headers.get(name))

    @staticmethod
    def _safe_request_url(method: str, url: str, params: dict[str, Any] | None) -> str:
        """Build the diagnostic URL once, redacting it before any request can fail."""
        try:
            prepared = requests.Request(method, url, params=params).prepare()
            return redact_url(prepared.url or url)
        except requests.RequestException:
            return redact_url(url)

    @staticmethod
    def _wait(delay: float, reason: str, method: str, safe_url: str, attempt: int, retries: int, *,
              cancel: threading.Event | None = None,
              state: DownloadState | None = None) -> None:
        """Sleep before a retry, logging any wait of LONG_WAIT_SECONDS or more."""
        if delay >= LONG_WAIT_SECONDS:
            resume_at = (datetime.now().astimezone() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            logger.warning(
                "waiting %.0fs until %s before retrying %s %s (%s, retry %d/%d)",
                delay, resume_at, method, safe_url, reason, attempt + 1, retries,
            )
        if state is not None:
            state.attempt = attempt + 1
            state.deadline = None
            state.prepared = None
            state.history.clear()
            raise RequestDeferred(time.monotonic() + delay)
        if cancel is None:
            time.sleep(delay)
        elif cancel.wait(delay):
            raise CancelledError

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
