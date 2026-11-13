"""Thread-local PDF clients with shared host admission and coordinated shutdown."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import CancelledError

from pauk.settings import Settings

from .base import HttpClient
from .rate_limit import RateLimit


class PdfDownloader:
    def __init__(self, config: Settings, *, client_factory: Callable[..., HttpClient] = HttpClient) -> None:
        self.config = config
        self._factory = client_factory
        self._local = threading.local()
        self._clients: list[HttpClient] = []
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _client(self) -> HttpClient:
        if self.cancelled:
            raise CancelledError
        if not hasattr(self._local, "client"):
            client = self._factory(self.config.request_timeout,
                                   default_rate_limit=RateLimit(max_concurrent=self.config.pdf_host_concurrency))
            with self._lock:
                if self.cancelled:
                    client.close()
                    raise CancelledError
                self._clients.append(client)
            self._local.client = client
        return self._local.client

    def get_bytes(self, url: str, *, retries: int | None = None, timeout: float | None = None,
                  deadline: float | None = None) -> bytes:
        return self._client().get_bytes(
            url, retries=self.config.pdf_retries if retries is None else retries,
            timeout=(self.config.pdf_connect_timeout, self.config.pdf_read_timeout if timeout is None else timeout),
            deadline=self.config.pdf_download_deadline if deadline is None else deadline,
            max_wait=self.config.pdf_max_retry_wait,
        )

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            clients = tuple(self._clients)
        for client in clients:
            client.cancel()

    def close(self) -> None:
        self.cancel()
        for client in self._clients:
            client.close()
