"""Thread-local PDF clients with shared host admission and coordinated shutdown."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import ExitStack
from typing import TypedDict

from pauk.settings import PDF_HOST_RPS, Settings

from .base import DownloadState, HttpClient
from .rate_limit import HostRateLimiter, RateLimit

PDF_RATE_LIMITER = HostRateLimiter(host_limits={host: RateLimit(rate) for host, rate in PDF_HOST_RPS.items()})


class _DownloadOptions(TypedDict):
    retries: int
    timeout: tuple[float, float]
    deadline: float
    max_wait: float
    state: DownloadState | None


class PdfDownloader:
    def __init__(self, config: Settings, *, client_factory: Callable[..., HttpClient] = HttpClient) -> None:
        self.config = config
        self._factory = client_factory
        self._local = threading.local()
        self._clients: list[HttpClient] = []
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._limiter = PDF_RATE_LIMITER
        self._limits = ExitStack()
        self._limits.enter_context(self._limiter.limit_concurrency(config.pdf_host_concurrency))

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _client(self) -> HttpClient:
        if self.cancelled:
            raise CancelledError
        if not hasattr(self._local, "client"):
            client = self._factory(self.config.request_timeout,
                                   default_rate_limit=RateLimit(), default_rate_limiter=self._limiter)
            with self._lock:
                if self.cancelled:
                    client.close()
                    raise CancelledError
                self._clients.append(client)
            self._local.client = client
        return self._local.client

    def get_bytes(self, url: str, *, retries: int | None = None, timeout: float | None = None,
                  deadline: float | None = None, state: DownloadState | None = None) -> bytes:
        return self._client().get_bytes(url, **self._options(retries, timeout, deadline, state))

    def get_bytes_with_url(self, url: str, *, retries: int | None = None, timeout: float | None = None,
                           deadline: float | None = None, state: DownloadState | None = None) -> tuple[bytes, str]:
        return self._client().get_bytes_with_url(url, **self._options(retries, timeout, deadline, state))

    def _options(self, retries: int | None, timeout: float | None,
                 deadline: float | None, state: DownloadState | None) -> _DownloadOptions:
        return _DownloadOptions(
            retries=self.config.pdf_retries if retries is None else retries,
            timeout=(self.config.pdf_connect_timeout, self.config.pdf_read_timeout if timeout is None else timeout),
            deadline=self.config.pdf_download_deadline if deadline is None else deadline,
            max_wait=self.config.pdf_max_retry_wait,
            state=state,
        )

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            clients = tuple(self._clients)
        for client in clients:
            client.cancel()

    def close(self) -> None:
        self.cancel()
        with self._lock, self._limits:
            for client in self._clients:
                client.close()
            self._clients.clear()
