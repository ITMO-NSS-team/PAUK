"""Real SDK client and HTTP-boundary checks using synthetic graph records only."""

import importlib.util
import json
import subprocess
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from pauk.mcp.service import Service
from tests.mcp_support import TEST_TOKEN, SyntheticReader, live_server

HAS_MCP = importlib.util.find_spec("mcp") is not None
if HAS_MCP:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from starlette.applications import Starlette

    from pauk.mcp.contracts import MCPError
    from pauk.mcp.server import Boundary, create_app


class _CloseSpyReader(SyntheticReader):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.close_calls = 0

    async def query(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
        """Fail if application resources were closed by an individual MCP session."""
        if self.closed:
            raise RuntimeError("Shared reader already closed")
        return await super().query(query, parameters)

    async def close(self) -> None:
        """Record application resource teardown."""
        self.closed = True
        self.close_calls += 1


@unittest.skipUnless(HAS_MCP, "Install the optional mcp extra for lifecycle tests")
class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_reader_closes_once_after_http_server_exit(self) -> None:
        reader = _CloseSpyReader()

        @asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncIterator[None]:
            try:
                yield
            finally:
                await reader.close()

        app = create_app(Service(reader, b"synthetic-key"), TEST_TOKEN, lifespan=lifespan)
        with live_server(app) as url:
            async with (
                httpx.AsyncClient(headers={"Authorization": "Bearer " + TEST_TOKEN}, timeout=5) as client,
                streamable_http_client(url, http_client=client) as (rx, tx, _),
                ClientSession(rx, tx) as session,
            ):
                await session.initialize()
                self.assertFalse(reader.closed)
                await session.list_tools()
                self.assertFalse(reader.closed)
                for name, arguments in (
                    ("search_publications", {"doi": "10.1234/pauk-test"}),
                    ("get_repository", {"repository_id": "r-code"}),
                ):
                    result = await session.call_tool(name, arguments)
                    self.assertFalse(result.isError)
                    self.assertFalse(reader.closed)
            self.assertEqual(reader.close_calls, 0)
        self.assertTrue(reader.closed)
        self.assertEqual(reader.close_calls, 1)


@unittest.skipUnless(HAS_MCP, "Install the optional mcp extra for protocol tests")
class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.reader = SyntheticReader()
        self.manager = live_server(create_app(Service(self.reader, b"synthetic-key"), TEST_TOKEN, tool_timeout=0.1))
        self.url = self.manager.__enter__()
        self.http = httpx.AsyncClient(headers={"Authorization": "Bearer " + TEST_TOKEN}, timeout=5)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        self.manager.__exit__(None, None, None)

    def _assert_tools_contract(self, tools: list[Any]) -> None:
        expected = {
            "search_publications": {"doi", "title_contains", "repository_contains", "page_size", "cursor"},
            "get_publication_repositories": {"publication_id", "page_size", "cursor"},
            "get_repository": {"repository_id"},
            "explain_link": {"publication_id", "repository_id", "page_size", "cursor"},
            "search_repositories": {"url", "query", "page_size", "cursor"},
            "get_repository_publications": {"repository_id", "relation", "page_size", "cursor"},
        }
        self.assertEqual({tool.name for tool in tools}, set(expected))
        for tool in tools:
            self.assertEqual(set(tool.inputSchema["properties"]), expected[tool.name])
            self.assertEqual(tool.outputSchema["properties"]["source_system"]["const"], "PAUK")
            self.assertEqual(tool.outputSchema["properties"]["retrieval_mode"]["const"], "cached_prepared_graph")

    def _assert_pauk_source(self, result: Any) -> None:
        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["source_system"], "PAUK")
        self.assertEqual(result.structuredContent["retrieval_mode"], "cached_prepared_graph")

    async def test_real_client_six_flat_tools_and_structured_results(self) -> None:
        async with (
            streamable_http_client(self.url, http_client=self.http) as (rx, tx, _),
            ClientSession(rx, tx) as session,
        ):
            await session.initialize()
            tools = (await session.list_tools()).tools
            self._assert_tools_contract(tools)
            search = next(t for t in tools if t.name == "search_publications")
            self.assertEqual(search.inputSchema["properties"]["page_size"]["maximum"], 25)
            result = await session.call_tool("search_publications", {"doi": "https://doi.org/10.1234/PAUK-TEST"})
            self._assert_pauk_source(result)
            self.assertEqual(result.structuredContent["items"][0]["publication_id"], "p-paper")
            result = await session.call_tool("search_publications", {"repository_contains": "pauk-test/code"})
            self._assert_pauk_source(result)
            self.assertEqual(result.structuredContent["items"][0]["publication_id"], "p-paper")
            result = await session.call_tool("search_publications", {"repository_contains": "Dependency"})
            self._assert_pauk_source(result)
            self.assertEqual(result.structuredContent["items"], [])
            result = await session.call_tool("get_publication_repositories", {"publication_id": "old-paper"})
            self._assert_pauk_source(result)
            self.assertEqual(result.structuredContent["publication_id"], "p-paper")
            self.assertEqual([r["repository_id"] for r in result.structuredContent["items"]], ["r-code"])
            result = await session.call_tool("get_repository", {"repository_id": "r-code"})
            self._assert_pauk_source(result)
            self.assertEqual(result.structuredContent["description"]["status"], "withheld")
            self.assertIn("not an access check", result.structuredContent["metadata_note"])
            self.assertIn("This call did not", result.structuredContent["verification_note"])
            result = await session.call_tool(
                "explain_link", {"publication_id": "p-paper", "repository_id": "r-dependency"}
            )
            self._assert_pauk_source(result)
            self.assertIn("not independent sources", result.structuredContent["evidence_note"])
            self.assertEqual(result.structuredContent["evidence"][0]["unit"], "extraction_occurrence")
            self.assertFalse(result.structuredContent["implements"])
            self.assertEqual(result.structuredContent["evidence"][0]["verdict"], "rejected")
            self.assertIsNone(result.structuredContent["evidence"][0]["confidence"])
            result = await session.call_tool(
                "search_repositories", {"url": "http://www.github.com/PAUK-TEST/code.git/"}
            )
            self._assert_pauk_source(result)
            self.assertEqual([r["repository_id"] for r in result.structuredContent["items"]], ["r-code"])
            result = await session.call_tool("search_repositories", {"query": "Dependency"})
            self._assert_pauk_source(result)
            self.assertEqual([r["repository_id"] for r in result.structuredContent["items"]], ["r-dependency"])
            result = await session.call_tool(
                "get_repository_publications", {"repository_id": "old-code", "relation": "implements"}
            )
            self._assert_pauk_source(result)
            self.assertEqual(result.structuredContent["repository_id"], "r-code")
            self.assertEqual([p["publication_id"] for p in result.structuredContent["items"]], ["p-paper"])
            result = await session.call_tool(
                "get_repository_publications", {"repository_id": "r-dependency", "relation": "mentions"}
            )
            self._assert_pauk_source(result)
            self.assertFalse(result.structuredContent["items"][0]["implements"])
            self.assertEqual(result.structuredContent["items"][0]["verdict"], "rejected")

    async def test_safe_errors_sdk_validation_unknown_alias_backend_and_timeout(self) -> None:
        async with (
            streamable_http_client(self.url, http_client=self.http) as (rx, tx, _),
            ClientSession(rx, tx) as session,
        ):
            await session.initialize()
            cases = [
                ("get_repository", {"repository_id": "PRIVATE_CANARY" * 100}, "INPUT"),
                ("get_repository", {"repository_id": "r-code", "secret": "PRIVATE_CANARY"}, "INPUT"),
                ("get_repository", {"repository_id": "missing"}, "NOT_FOUND"),
                ("get_repository", {"repository_id": "ambiguous-repo"}, "AMBIGUOUS_ID"),
                ("search_publications", {"doi": "x", "cursor": "PRIVATE_CANARY"}, "CURSOR"),
                ("search_repositories", {"url": "https://private.example.org/PRIVATE_CANARY"}, "INPUT"),
                ("search_repositories", {"query": "abc", "cursor": "PRIVATE_CANARY"}, "CURSOR"),
                ("get_repository_publications", {"repository_id": "PRIVATE_CANARY" * 100}, "INPUT"),
                ("get_repository_publications", {"repository_id": "missing"}, "NOT_FOUND"),
                ("get_repository_publications", {"repository_id": "r-code", "cursor": "PRIVATE_CANARY"}, "CURSOR"),
            ]
            for name, arguments, code in cases:
                result = await session.call_tool(name, arguments)
                self.assertTrue(result.isError)
                self.assertEqual(result.content[0].text, code)
            for error, code in (
                (RuntimeError("SECRET_PRIVATE_BACKEND"), "INTERNAL"),
                (MCPError("BACKEND"), "BACKEND"),
            ):
                self.reader.error = error
                result = await session.call_tool("get_repository", {"repository_id": "r-code"})
                self.assertEqual(result.content[0].text, code)
            self.reader.error = None
            self.reader.delay = 2
            result = await session.call_tool("get_repository", {"repository_id": "r-code"})
            self.assertEqual(result.content[0].text, "TIMEOUT")
            self.assertTrue(self.reader.cancelled)

    async def test_auth_before_body_or_db_and_dns_rebinding(self) -> None:
        for authorization in (None, "Bearer wrong"):
            async with httpx.AsyncClient() as client:
                headers = {} if authorization is None else {"Authorization": authorization}
                response = await client.post(self.url, content=b"x" * 10000, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(self.reader.calls, 0)
        response = await self.http.post(self.url, content=b"x" * 8193)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.reader.calls, 0)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        response = await self.http.post(self.url, json=request, headers={**headers, "Host": "evil.example"})
        self.assertEqual(response.status_code, 421)
        response = await self.http.post(self.url, json=request, headers={**headers, "Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reader.calls, 0)

    async def test_sdk_envelope_validation_never_echoes_input_or_logs(self) -> None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        messages = [
            {"method": {"secret": "PRIVATE_CANARY"}},
            {"method": "ping", "id": {"secret": "PRIVATE_CANARY"}},
            {"method": "ping", "id": True},
            {"method": "PRIVATE_CANARY"},
            {"method": "tools/call", "params": {"name": "get_repository", "arguments": "PRIVATE_CANARY"}},
            {"method": "tools/call", "params": {"name": ["PRIVATE_CANARY"], "arguments": {}}},
            {"method": "tools/call", "params": {"name": "PRIVATE_CANARY", "arguments": {}}},
            {
                "method": "tools/call",
                "params": {"name": "get_repository", "arguments": {"repository_id": ["PRIVATE_CANARY"]}},
            },
        ]
        notifications = [
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": "PRIVATE_CANARY"},
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": {"secret": "PRIVATE_CANARY"}},
            },
            {"jsonrpc": "2.0", "method": {"secret": "PRIVATE_CANARY"}},
        ]
        with self.assertLogs(level="INFO") as captured:
            for message in [{"jsonrpc": "2.0", "id": 7, **m} for m in messages] + notifications:
                response = await self.http.post(self.url, headers=headers, json=message)
                self.assertNotIn("PRIVATE_CANARY", response.text)
                self.assertLessEqual(len(response.content), 65536)
        self.assertNotIn("PRIVATE_CANARY", "\n".join(captured.output))
        self.assertEqual(self.reader.calls, 0)

    async def test_wire_response_cap_unicode_and_framing(self) -> None:
        rows = [{"id": f"p-{i}", "title": "\U0001f600" * 1001, "doi": None, "year": 2026} for i in range(25)]

        class LargeReader(SyntheticReader):
            async def query(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
                """Return an offset-sliced page of large Unicode publications."""
                offset = int(parameters.get("offset", 0))
                limit = int(parameters.get("limit", 25))
                return rows[offset : offset + limit]

        manager = live_server(create_app(Service(LargeReader(), b"key"), TEST_TOKEN))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        with manager as url:
            first_request = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_publications",
                    "arguments": {"title_contains": "abc", "page_size": 25},
                },
            }
            first_response = await self.http.post(url, json=first_request, headers=headers)
            self.assertEqual(first_response.status_code, 200)
            self.assertLessEqual(len(first_response.content), 65536)
            first_page = first_response.json()["result"]["structuredContent"]
            first_count = len(first_page["items"])
            self.assertGreaterEqual(first_count, 1)
            self.assertLess(first_count, 25)
            self.assertEqual(first_page["items"][0]["publication_id"], "p-0")
            self.assertIsNotNone(first_page["next_cursor"])

            next_request = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_publications",
                    "arguments": {"title_contains": "abc", "page_size": 2, "cursor": first_page["next_cursor"]},
                },
            }
            next_response = await self.http.post(url, json=next_request, headers=headers)
            self.assertEqual(next_response.status_code, 200)
            self.assertLessEqual(len(next_response.content), 65536)
            next_page = next_response.json()["result"]["structuredContent"]
            self.assertEqual(len(next_page["items"]), 2)
            self.assertEqual(next_page["items"][0]["publication_id"], f"p-{first_count}")
            self.assertEqual(next_page["items"][1]["publication_id"], f"p-{first_count + 1}")

    async def test_json_like_string_arguments_are_not_reparsed(self) -> None:
        title = "null hypothesis [1]"

        class LiteralStringReader(SyntheticReader):
            async def query(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
                """Return the literal title fixture when every query token appears."""
                if "title CONTAINS token" in query:
                    tokens = [t for t in str(parameters["query"]).split(" ") if t]
                    if all(token in title for token in tokens):
                        return [{"id": "p-null", "title": title, "doi": None, "year": 2026}]
                    return []
                if "MATCH (n:Repository) WHERE n.id" in query or "merged_ids" in query:
                    return []
                if "r.name IS NOT NULL AND r.name IS :: STRING" in query:
                    return []
                return []

        manager = live_server(create_app(Service(LiteralStringReader(), b"key"), TEST_TOKEN))
        with manager as url:
            async with (
                streamable_http_client(url, http_client=self.http) as (rx, tx, _),
                ClientSession(rx, tx) as session,
            ):
                await session.initialize()
                for fragment in ("null", "[1]", "null hypothesis"):
                    result = await session.call_tool("search_publications", {"title_contains": fragment})
                    self._assert_pauk_source(result)
                    self.assertEqual([item["publication_id"] for item in result.structuredContent["items"]], ["p-null"])
                for arguments in (
                    {"title_contains": "true"},
                    {"title_contains": '{"a": 1}'},
                    {"repository_contains": "null"},
                    {"doi": "null"},
                ):
                    result = await session.call_tool("search_publications", arguments)
                    self._assert_pauk_source(result)
                    self.assertEqual(result.structuredContent["items"], [])
                repo_null = await session.call_tool("search_repositories", {"query": "null"})
                self._assert_pauk_source(repo_null)
                self.assertEqual(repo_null.structuredContent["items"], [])
                missing = await session.call_tool("get_repository", {"repository_id": "null"})
                self.assertTrue(missing.isError)
                self.assertEqual(missing.content[0].text, "NOT_FOUND")


class AdkRunnerTests(unittest.TestCase):
    def test_invoke_client_keeps_symlinked_venv_python_path(self) -> None:
        from tests.integration.adk.run import invoke_client

        python_path = Path("fake-venv/bin/python")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"status":"passed"}\n', stderr="")
        with (
            mock.patch.object(Path, "resolve", side_effect=AssertionError("must not resolve symlinks")),
            mock.patch("tests.integration.adk.run.subprocess.run", return_value=completed) as run_mock,
        ):
            report = invoke_client(python_path, "http://127.0.0.1:1/mcp", "http://127.0.0.1:2/mcp", "tok")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(run_mock.call_args.args[0][0], str(python_path.absolute()))


@unittest.skipUnless(HAS_MCP, "Install the optional mcp extra for boundary tests")
class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_does_not_read_body(self) -> None:
        calls = 0

        async def app(*args: Any) -> None:
            nonlocal calls
            calls += 1

        async def receive() -> dict[str, Any]:
            self.fail("Unauthorized request body was read")

        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await Boundary(app, TEST_TOKEN)({"type": "http", "headers": [], "method": "POST"}, receive, send)
        self.assertEqual(calls, 0)
        self.assertEqual(sent[0]["status"], 401)

    async def test_escaping_and_exact_response_boundary(self) -> None:
        for size in (65536, 65537):
            payload = b'"' + b"x" * (size - 2) + b'"'

            async def app(scope: Any, receive: Any, send: Any, payload: bytes = payload) -> None:
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": payload[:32000], "more_body": True})
                await send({"type": "http.response.body", "body": payload[32000:]})

            transport = httpx.ASGITransport(app=Boundary(app, TEST_TOKEN))
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
                response = await client.post(
                    "/mcp",
                    headers={"Authorization": "Bearer " + TEST_TOKEN},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                )
                self.assertLessEqual(len(response.content), 65536)
                self.assertEqual(response.status_code, 200 if size == 65536 else 500)
                json.loads(response.content)


@unittest.skipUnless(HAS_MCP, "Install the optional mcp extra for submission protocol tests")
class SubmissionProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_submission_tool_boundary_and_configuration(self) -> None:
        import mongomock
        from mcp.server.fastmcp.exceptions import ToolError

        from pauk.mcp.server import SafeMCP, _check_configuration, register_tools
        from pauk.mcp.submissions import MongoSuggestionStore
        from pauk.settings import Settings
        from pauk.storage import link_suggestions

        # SafeMCP directly (without Boundary) refuses submit_link_evidence when disabled.
        disabled_service = Service(SyntheticReader(), b"key")
        safe_mcp = SafeMCP(disabled_service, name="disabled-test")
        register_tools(safe_mcp, disabled_service)
        with self.assertRaises(ToolError) as direct_err:
            await safe_mcp.call_tool(
                "submit_link_evidence",
                {
                    "publication_id": "p-paper",
                    "proposed_relation": "implements",
                    "repository_id": "r-code",
                    "rationale": "Direct SafeMCP call must reject when disabled.",
                },
            )
        self.assertEqual(str(direct_err.exception), "INPUT")

        # Default server refuses submit_link_evidence cleanly at the boundary with INPUT.
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + TEST_TOKEN}, timeout=5) as http:
            with live_server(create_app(Service(SyntheticReader(), b"key"), TEST_TOKEN)) as url:
                disabled_resp = await http.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "submit_link_evidence",
                            "arguments": {
                                "publication_id": "p-paper",
                                "proposed_relation": "implements",
                                "repository_id": "r-code",
                                "rationale": "Should be rejected when disabled.",
                            },
                        },
                    },
                )
                self.assertEqual(disabled_resp.status_code, 200)
                self.assertEqual(
                    disabled_resp.json()["result"],
                    {"content": [{"type": "text", "text": "INPUT"}], "isError": True},
                )

            # With suggestion_store enabled, 7 tools are exposed and calls persist into Mongo.
            db = mongomock.MongoClient()["pauk_mcp_server_submit"]
            app = create_app(
                Service(SyntheticReader(), b"key", suggestion_store=MongoSuggestionStore(db)),
                TEST_TOKEN,
            )
            with live_server(app) as url:
                async with (
                    streamable_http_client(url, http_client=http) as (rx, tx, _),
                    ClientSession(rx, tx) as session,
                ):
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self.assertIn("submit_link_evidence", {t.name for t in tools})
                    self.assertEqual(len(tools), 7)
                    result = await session.call_tool(
                        "submit_link_evidence",
                        {
                            "publication_id": "old-paper",
                            "proposed_relation": "implements",
                            "repository_url": "https://github.com/org/new-repo",
                            "rationale": "Found author code link in official abstract.",
                        },
                    )
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent["status"], "queued")
                    self.assertEqual(result.structuredContent["publication_id"], "p-paper")
                    self.assertEqual(result.structuredContent["repository_url"], "https://github.com/org/new-repo")
                    self.assertEqual(link_suggestions.count(db, status=link_suggestions.PENDING), 1)

        base_kwargs = {
            "mcp_neo4j_uri": "bolt://127.0.0.1:7688",
            "mcp_neo4j_user": "neo4j",
            "mcp_neo4j_password": "secret",
            "mcp_neo4j_database": "neo4j",
            "mcp_dataset_policy": "isolated",
            "mcp_token": TEST_TOKEN,
        }
        _check_configuration(Settings(**base_kwargs))  # Disabled submissions needs no Mongo settings.

        for bad_mongo in (
            {},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27017", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:027018", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018\n", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:0", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://@127.0.0.1:27018", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://evil.example:27018", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://user:pass@127.0.0.1:27018", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018/path", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018?authSource=admin", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018#frag", "mcp_mongo_db": "pauk"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018", "mcp_mongo_db": ""},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018", "mcp_mongo_db": " pauk "},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018", "mcp_mongo_db": "bad/db"},
            {"mcp_mongo_uri": "mongodb://127.0.0.1:27018", "mcp_mongo_db": "bad\x7fdb"},
        ):
            with self.subTest(bad_mongo=bad_mongo), self.assertRaises(SystemExit):
                _check_configuration(Settings(**base_kwargs, mcp_submissions_enabled=True, **bad_mongo))

        with self.assertRaises(SystemExit):
            _check_configuration(Settings(**{**base_kwargs, "mcp_neo4j_uri": "bolt://127.0.0.1:07688"}))
        with self.assertRaises(SystemExit):
            _check_configuration(Settings(**{**base_kwargs, "mcp_neo4j_uri": "bolt://127.0.0.1:7688\n"}))

        _check_configuration(
            Settings(
                **base_kwargs,
                mcp_submissions_enabled=True,
                mcp_mongo_uri="mongodb://127.0.0.1:27018",
                mcp_mongo_db="pauk_isolated",
            )
        )

    async def test_submission_store_refuses_writes_when_index_creation_fails(self) -> None:
        import mongomock
        from pymongo.errors import OperationFailure

        from pauk.mcp.contracts import MCPError
        from pauk.mcp.submissions import MongoSuggestionStore
        from pauk.storage import link_suggestions

        db = mongomock.MongoClient()["pauk_mcp_index_fail"]
        store = MongoSuggestionStore(db)
        with (
            mock.patch("pauk.storage.link_suggestions.ensure_indexes", side_effect=OperationFailure("index error")),
            self.assertRaises(MCPError) as ctx,
        ):
            await store.submit(
                publication_id="p-paper",
                proposed_relation="implements",
                repository_url="https://github.com/org/repo",
                rationale="Index initialization must gate writes.",
            )
        self.assertEqual(ctx.exception.code, "BACKEND")
        self.assertEqual(db[link_suggestions.COLLECTION].count_documents({}), 0)

        # Once index initialization succeeds, subsequent submissions persist cleanly.
        sid, state = await store.submit(
            publication_id="p-paper",
            proposed_relation="implements",
            repository_url="https://github.com/org/repo",
            rationale="Index initialization must gate writes.",
        )
        self.assertTrue(sid.startswith("sug_"))
        self.assertEqual(state, "queued")
