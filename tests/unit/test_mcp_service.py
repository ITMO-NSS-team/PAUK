"""Offline contracts, graph semantics and data minimization."""

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

from pauk.mcp import reader as queries
from pauk.mcp.contracts import (
    Cursors,
    Evidence,
    ExplainRequest,
    MCPError,
    PublicationRequest,
    RepositoryRequest,
    SearchRequest,
    Text,
    normalize_doi,
    safe_url,
)
from pauk.mcp.reader import Neo4jReader, resolve
from pauk.mcp.service import Service


class StubReader:
    """Return predetermined projected records without network access."""

    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def query(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
        """Record query text and parameters."""
        self.calls.append((query, parameters))
        return self.results.pop(0)


class ContractsTests(unittest.TestCase):
    def test_search_exclusivity_and_bounds(self) -> None:
        for arguments in (
            {},
            {"doi": "x", "title_contains": "abc"},
            {"doi": "x", "repository_contains": "abc"},
            {"title_contains": "abc", "repository_contains": "def"},
            {"doi": "x", "title_contains": "abc", "repository_contains": "def"},
            {"doi": "doi:"},
            {"doi": "https://doi.org/"},
            {"title_contains": "ab"},
            {"repository_contains": "ab"},
            {"repository_contains": "   "},
            {"doi": "x", "page_size": 26},
            {"doi": "x", "page_size": True},
            {"doi": "x", "page_size": "10"},
            {"doi": "x", "unexpected": "secret"},
            {"doi": "x", "source_system": "PAUK"},
            {"doi": "x", "retrieval_mode": "cached_prepared_graph"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                SearchRequest.model_validate(arguments)
        self.assertEqual(normalize_doi(" HTTPS://DOI.ORG/10.A/ABC "), "10.a/abc")
        self.assertEqual(SearchRequest(doi="x").page_size, 10)
        with self.assertRaises(ValidationError):
            ExplainRequest(publication_id="p", repository_id="r", page_size=11)
        with self.assertRaises(ValidationError):
            RepositoryRequest(repository_id="x" * 257)

    def test_text_invariants(self) -> None:
        for status in ("withheld", "unavailable"):
            with self.assertRaises(ValidationError):
                Text(value="PRIVATE_CANARY", status=status)
            with self.assertRaises(ValidationError):
                Text(status=status, truncated=True)
        with self.assertRaises(ValidationError):
            Text(status="available")

    def test_safe_urls(self) -> None:
        self.assertEqual(safe_url("https://github.com/org/repo"), "https://github.com/org/repo")
        for value in (
            "file:///private/key",
            "https://github.com/a?signature=SECRET",
            "http://127.0.0.1/x",
            "https://private.example.org/repo",
            "https://user:SECRET@github.com/a",
            "https://github.com:8080/a",
            "https://github.com/a\n",
            "https://github.com/" + "x" * 2000,
        ):
            with self.subTest(value=value):
                self.assertIsNone(safe_url(value))

    def test_cursor_request_binding_and_tampering(self) -> None:
        cursors = Cursors(b"synthetic-test-key")
        request = {"tool": "search", "query": "abc", "page_size": 10}
        cursor = cursors.encode(request, 10)
        self.assertEqual(cursors.decode(cursor, request), 10)
        for bad, binding in (
            (cursor, {**request, "page_size": 5}),
            (cursor[:-2] + "xx", request),
            ("not base64!", request),
            ("x" * 1025, request),
            (cursors.encode(request, -1), request),
            (cursors.encode(request, True), request),
        ):
            with self.subTest(bad=bad), self.assertRaises(MCPError) as caught:
                cursors.decode(bad, binding)
            self.assertEqual(caught.exception.code, "CURSOR")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_direct_precedes_alias_and_ambiguity(self) -> None:
        reader = StubReader([[{"id": "p"}]])
        self.assertEqual(await resolve(reader, "Publication", "p"), "p")
        self.assertEqual(len(reader.calls), 1)
        reader = StubReader([[], [{"id": "canonical"}]])
        self.assertEqual(await resolve(reader, "Repository", "old"), "canonical")
        for rows, code in (([[], []], "NOT_FOUND"), ([[], [{"id": "a"}, {"id": "b"}]], "AMBIGUOUS_ID")):
            with self.assertRaises(MCPError) as caught:
                await resolve(StubReader(rows), "Publication", "old")
            self.assertEqual(caught.exception.code, code)

    async def test_search_literal_normalized_and_paginated(self) -> None:
        reader = StubReader([[{"id": "p1", "title": "x" * 1001, "doi": None, "year": 2026}, {"id": "p2"}]])
        service = Service(reader, b"synthetic-key")
        request = SearchRequest(title_contains="AbC.*", page_size=1)
        page = await service.search_publications(request)
        self.assertTrue(page.items[0].title.truncated)
        self.assertEqual(len(page.items[0].title.value), 1000)
        self.assertIsNotNone(page.next_cursor)
        self.assertEqual(reader.calls[0][0], queries.SEARCH_TITLE)
        self.assertEqual(reader.calls[0][1]["query"], "abc.*")
        reader.results = [[]]
        await service.search_publications(request.model_copy(update={"cursor": page.next_cursor}))
        self.assertEqual(reader.calls[-1][1]["offset"], 1)
        reader.results = [[]]
        await service.search_publications(SearchRequest(doi="https://doi.org/10.A/b"))
        self.assertEqual(reader.calls[-1][0], queries.SEARCH_DOI)
        self.assertEqual(reader.calls[-1][1]["query"], "10.a/b")

    async def test_search_multi_token_conjunctive_order_and_empty(self) -> None:
        from tests.mcp_support import SyntheticReader

        service = Service(SyntheticReader(), b"synthetic-key")
        found = await service.search_publications(SearchRequest(title_contains="Synthetic paper"))
        self.assertEqual([item.publication_id for item in found.items], ["p-paper"])
        empty = await service.search_publications(SearchRequest(title_contains="unmatched synthetic string"))
        self.assertEqual(empty.items, [])

    async def test_search_repository_mode_cursor_binding_and_excludes_mentions(self) -> None:
        from tests.mcp_support import SyntheticReader

        reader = StubReader([[{"id": "p1", "title": "First paper", "doi": None, "year": 2026}, {"id": "p2"}]])
        service = Service(reader, b"synthetic-key")
        request = SearchRequest(repository_contains="  Synthetic   Code ", page_size=1)
        page = await service.search_publications(request)
        self.assertEqual([item.publication_id for item in page.items], ["p1"])
        self.assertIsNotNone(page.next_cursor)
        self.assertEqual(reader.calls[0][0], queries.SEARCH_REPOSITORY)
        self.assertEqual(reader.calls[0][1]["query"], "synthetic code")
        self.assertNotIn("MENTIONS_LINK", queries.SEARCH_REPOSITORY)
        self.assertNotIn("r.description", queries.SEARCH_REPOSITORY)
        reader.results = [[]]
        await service.search_publications(request.model_copy(update={"cursor": page.next_cursor}))
        self.assertEqual(reader.calls[-1][1]["offset"], 1)
        with self.assertRaises(MCPError) as caught:
            await service.search_publications(SearchRequest(title_contains="synthetic code", cursor=page.next_cursor))
        self.assertEqual(caught.exception.code, "CURSOR")

        synthetic_service = Service(SyntheticReader(), b"synthetic-key")
        by_url = await synthetic_service.search_publications(SearchRequest(repository_contains="pauk-test/code"))
        self.assertEqual([item.publication_id for item in by_url.items], ["p-paper"])
        by_name = await synthetic_service.search_publications(SearchRequest(repository_contains="Synthetic code"))
        self.assertEqual([item.publication_id for item in by_name.items], ["p-paper"])
        mention_only = await synthetic_service.search_publications(SearchRequest(repository_contains="Dependency"))
        self.assertEqual(mention_only.items, [])

    async def test_empty_author_code_is_unknown_and_only_implements(self) -> None:
        reader = StubReader([[{"id": "p"}], []])
        result = await Service(reader, b"key").get_publication_repositories(PublicationRequest(publication_id="p"))
        self.assertEqual(result.items, [])
        self.assertIn("unknown", result.coverage)
        self.assertEqual(reader.calls[-1][0], queries.REPOSITORIES)
        self.assertNotIn("MENTIONS_LINK", reader.calls[-1][0])

    async def test_private_description_withheld_and_actual_update_field(self) -> None:
        row = {
            "id": "r",
            "url": "https://github.com/o/r",
            "name": "repo",
            "description_present": True,
            "description": "PRIVATE_CANARY",
            "last_updated": "2026-01-02",
            "updated_at": "SECRET",
        }
        reader = StubReader([[{"id": "r"}], [row]])
        result = await Service(reader, b"key").get_repository(RepositoryRequest(repository_id="r"))
        self.assertEqual(result.description.status, "withheld")
        self.assertNotIn("PRIVATE_CANARY", result.model_dump_json())
        self.assertEqual(result.last_updated, "2026-01-02")
        self.assertIsNone(result.archived)
        self.assertIsNone(result.language.value)
        self.assertFalse(reader.calls[-1][1]["text"])
        reader.results = [[{"id": "r"}], [row]]
        result = await Service(reader, b"key", safe_synthetic_text=True).get_repository(
            RepositoryRequest(repository_id="r")
        )
        self.assertEqual(result.description.value, "PRIVATE_CANARY")

    async def _explain(self, row: dict[str, Any], *, allowed: bool = True) -> Evidence:
        reader = StubReader([[{"id": "p"}], [{"id": "r"}], [], [row]])
        result = await Service(reader, b"key", safe_synthetic_text=allowed).explain_link(
            ExplainRequest(publication_id="p", repository_id="r"),
        )
        self.assertFalse(result.implements)
        return result.evidence[0]

    async def test_evidence_verdicts_confidence_and_missing_page(self) -> None:
        for relevant, verdict in ((True, "accepted"), (False, "rejected"), (None, "unknown")):
            evidence = await self._explain({"aligned": True, "relevant": relevant, "page": 0, "context": "synthetic"})
            self.assertEqual(evidence.verdict, verdict)
            self.assertEqual(evidence.pages, [])
            self.assertIsNone(evidence.source_kind)
            self.assertIsNone(evidence.source_url)
            self.assertIsNone(evidence.confidence)
        evidence = await self._explain({"aligned": True, "page": 0, "source": "abstract", "confidence": 0.0})
        self.assertEqual(evidence.pages, [0])
        self.assertEqual(evidence.confidence, 0.0)
        for score in (True, float("nan"), float("inf"), -0.1, 1.1, "high"):
            evidence = await self._explain({"aligned": True, "confidence": score})
            self.assertIsNone(evidence.confidence)

    async def test_evidence_privacy_truncation_and_misalignment(self) -> None:
        row = {
            "aligned": True,
            "context": "PRIVATE_CANARY",
            "context_present": True,
            "reason": "PRIVATE_REASON",
            "reason_present": True,
            "source": "pdf_text",
            "page": 3,
        }
        evidence = await self._explain(row, allowed=False)
        self.assertEqual(evidence.context.status, "withheld")
        self.assertEqual(evidence.reason.status, "withheld")
        self.assertNotIn("PRIVATE_", evidence.model_dump_json())
        evidence = await self._explain({**row, "context": "x" * 1001, "reason": "y" * 501})
        self.assertTrue(evidence.context.truncated)
        self.assertTrue(evidence.reason.truncated)
        self.assertEqual(len(evidence.reason.value), 500)
        evidence = await self._explain({**row, "aligned": False, "context_present": False})
        self.assertEqual(evidence.status, "unavailable")
        self.assertIsNone(evidence.context.value)
        self.assertEqual(evidence.pages, [])
        self.assertIsNone(evidence.source_kind)

    async def test_classification_status_is_an_enum_not_public_free_text(self) -> None:
        for value in (
            "PRIVATE_CANARY user@example.invalid /private/key",
            42,
            ["classified"],
            {"secret": "PRIVATE_CANARY"},
        ):
            evidence = await self._explain({"aligned": True, "classification_status": value}, allowed=False)
            self.assertEqual(evidence.classification_status.status, "unavailable")
            self.assertNotIn("PRIVATE_CANARY", evidence.model_dump_json())
        for value in ("pending", "classified", "failed"):
            evidence = await self._explain({"aligned": True, "classification_status": value}, allowed=False)
            self.assertEqual(evidence.classification_status.value, value)
        evidence = await self._explain({"aligned": True, "source": ["abstract"], "page": 0})
        self.assertIsNone(evidence.source_kind)
        self.assertEqual(evidence.pages, [])

    async def test_pair_of_existing_objects_without_relation(self) -> None:
        reader = StubReader([[{"id": "p"}], [{"id": "r"}], [], []])
        result = await Service(reader, b"key").explain_link(ExplainRequest(publication_id="p", repository_id="r"))
        self.assertFalse(result.implements)
        self.assertEqual(result.evidence, [])

    async def test_oversized_id_is_not_truncated(self) -> None:
        reader = StubReader([[{"id": "x" * 257}]])
        with self.assertRaises(MCPError):
            await Service(reader, b"key").get_repository(RepositoryRequest(repository_id="r"))


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_deadline_cancels_session(self) -> None:
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        cancelled = asyncio.Event()

        async def slow(*args: object) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        session.run = slow
        driver = MagicMock()
        driver.session.return_value = session
        reader = Neo4jReader(driver, "synthetic", query_timeout=0.02)
        with self.assertRaises(MCPError) as caught:
            await reader.query(queries.SEARCH_TITLE, {"query": "abc", "offset": 0, "limit": 10})
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertTrue(cancelled.is_set())
        session.cancel.assert_called_once()
        session.__aexit__.assert_awaited_once()

    async def test_external_cancellation_cleans_query(self) -> None:
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.run = AsyncMock(side_effect=asyncio.CancelledError)
        driver = MagicMock()
        driver.session.return_value = session
        with self.assertRaises(asyncio.CancelledError):
            await Neo4jReader(driver, "synthetic").query(queries.IMPLEMENTS, {})
        session.cancel.assert_called_once()
