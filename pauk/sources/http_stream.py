"""Expose the connected socket before urllib3 reads response headers."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from functools import cache
from socket import socket

from requests import Session
from requests.adapters import HTTPAdapter

_watch_socket: ContextVar[Callable[[socket], AbstractContextManager[None]] | None] = ContextVar(
    "watch_response_socket", default=None,
)


class _WatchedConnection:
    def getresponse(self):
        watch = _watch_socket.get()
        if watch is not None and self.sock is not None:
            with watch(self.sock):
                return super().getresponse()
        return super().getresponse()


@cache
def _connection_class(original: type) -> type:
    class WatchedConnection(_WatchedConnection, original):
        pass

    return WatchedConnection


class _StreamingAdapter(HTTPAdapter):
    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        pool = super().get_connection_with_tls_context(request, verify, proxies=proxies, cert=cert)
        if not issubclass(pool.ConnectionCls, _WatchedConnection):
            pool.ConnectionCls = _connection_class(pool.ConnectionCls)
        return pool


@contextmanager
def watch_headers(session: Session, url: str, watch: Callable[[socket], AbstractContextManager[None]]) -> Iterator[None]:
    """Use the socket watcher with the default Requests transport during this send."""
    adapter = session.get_adapter(url)
    if type(adapter) is HTTPAdapter:
        prefix = next(key for key in session.adapters if url.lower().startswith(key.lower()))
        session.mount(prefix, _StreamingAdapter(
            pool_connections=adapter._pool_connections,
            pool_maxsize=adapter._pool_maxsize,
            pool_block=adapter._pool_block,
            max_retries=adapter.max_retries,
        ))
        adapter.close()
    token = _watch_socket.set(watch)
    try:
        yield
    finally:
        _watch_socket.reset(token)
