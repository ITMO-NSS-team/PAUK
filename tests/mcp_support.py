"""Reusable synthetic MCP fixtures; no databases or external requests."""

import asyncio
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pauk.mcp import reader as queries

TEST_TOKEN = "pauk-synthetic-machine-token-0000000001"


class SyntheticReader:
    """Small deterministic graph for protocol clients and security probes."""

    def __init__(self) -> None:
        self.calls = 0
        self.delay = 0.0
        self.cancelled = False
        self.error: Exception | None = None

    async def query(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
        """Evaluate the prototype's fixed queries over synthetic records."""
        self.calls += 1
        if self.error:
            raise self.error
        if self.delay:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if query in (queries.RESOLVE["Publication"][0], queries.RESOLVE["Repository"][0]):
            identifier = parameters["id"]
            return [{"id": identifier}] if identifier in ("p-paper", "r-code", "r-dependency") else []
        if query in (queries.RESOLVE["Publication"][1], queries.RESOLVE["Repository"][1]):
            if parameters["id"] == "old-paper":
                return [{"id": "p-paper"}]
            return [{"id": "r-code"}, {"id": "r-dependency"}] if parameters["id"] == "ambiguous-repo" else []
        if query in (queries.SEARCH_TITLE, queries.SEARCH_DOI):
            match = (
                parameters["query"] == "10.1234/pauk-test"
                if query == queries.SEARCH_DOI
                else (str(parameters["query"]) in "synthetic pauk paper")
            )
            return (
                [{"id": "p-paper", "title": "Synthetic PAUK paper", "doi": "10.1234/pauk-test", "year": 2026}]
                if match
                else []
            )
        return self._metadata(query, parameters)

    def _metadata(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
        if query == queries.REPOSITORIES:
            return [{"id": "r-code", "name": "Synthetic code", "url": "https://github.com/pauk-test/code"}]
        if query == queries.REPOSITORY:
            return [
                {
                    "id": parameters["repository_id"],
                    "name": "Synthetic code",
                    "url": "https://github.com/pauk-test/code",
                    "description": "PRIVATE_CANARY" if parameters["text"] else None,
                    "description_present": True,
                    "language": "Python",
                    "license": "MIT",
                    "archived": False,
                    "last_updated": "2026-01-02",
                }
            ]
        if query == queries.IMPLEMENTS:
            return [{"implements": True}] if parameters["repository_id"] == "r-code" else []
        if query == queries.EVIDENCE:
            return [
                {
                    "aligned": True,
                    "context": "Synthetic evidence" if parameters["text"] else None,
                    "context_present": True,
                    "reason_present": True,
                    "reason": "Synthetic reason" if parameters["text"] else None,
                    "source": "abstract",
                    "page": 0,
                    "confidence": None,
                    "relevant": parameters["repository_id"] == "r-code",
                    "classification_status": "classified",
                }
            ]
        raise AssertionError("Unexpected fixture query")


@contextmanager
def live_server(app: Any) -> Iterator[str]:
    """Run a real loopback HTTP server on an ephemeral socket and join it on exit."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("Synthetic HTTP server failed to start")
            threading.Event().wait(0.01)
        if not server.started:
            raise RuntimeError("Synthetic HTTP server startup timed out")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Synthetic HTTP server did not stop")
