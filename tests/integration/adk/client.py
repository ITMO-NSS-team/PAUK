"""Проверка настоящего ADK MCP-клиента без модели и производственных данных."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if not __debug__:
    raise SystemExit("Optimized Python is disabled: harness acceptance checks require assertions")

os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

import httpx
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.tool_context import ToolContext

TOOLS = frozenset(
    {
        "search_publications",
        "get_publication_repositories",
        "get_repository",
        "explain_link",
        "search_repositories",
        "get_repository_publications",
    }
)
KINDS = frozenset({"valid", "empty", "not_found", "invalid_args"})
LOGGER = logging.getLogger("pauk_adk_harness")


def local_url(value: str) -> str:
    """Разрешает только явные loopback HTTP-адреса без реквизитов."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.port
        or parsed.path not in {"/mcp", "/mcp/"}
    ):
        raise argparse.ArgumentTypeError("Expected explicit loopback HTTP URL with port and /mcp")
    return value


def safe_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Отключает перенаправления, внешние прокси и сетевое журналирование."""
    return httpx.AsyncClient(
        headers=headers, timeout=timeout or httpx.Timeout(5), auth=auth, trust_env=False, follow_redirects=False
    )


async def context() -> ToolContext:
    """Создаёт настоящий контекст ADK без агента, модели и ADC."""
    service = InMemorySessionService()
    session = await service.create_session(app_name="pauk_harness", user_id="synthetic")
    return ToolContext(InvocationContext(session_service=service, session=session, invocation_id="synthetic-test"))


def toolset(url: str, token: str, filtered: bool = False) -> McpToolset:
    """Создаёт клиент с машинным Bearer-заголовком и ограниченными таймаутами."""
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=local_url(url),
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
            sse_read_timeout=30.0,
            httpx_client_factory=safe_client,
        ),
        tool_filter=["search_publications"] if filtered else None,
    )


def subset(actual: Any, expected: Any, path: str = "result") -> None:
    """Проверяет ожидаемое подмножество, сохраняя точный порядок списков."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object"
        for key, value in expected.items():
            assert key in actual, f"{path}: missing expected key"
            subset(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), f"{path}: list size mismatch"
        for index, value in enumerate(expected):
            subset(actual[index], value, f"{path}[{index}]")
    else:
        assert actual == expected, f"{path}: value mismatch"


def pointer(value: Any, path: str) -> Any:
    """Читает JSON Pointer из полного MCP-конверта."""
    assert path == "" or path.startswith("/"), "Expected JSON Pointer"
    for part in path.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def inspect_result(result: Any, case: dict[str, Any], token: str) -> None:
    """Проверяет конверт MCP и ожидания, не выводя содержимое ответов."""
    assert isinstance(result, dict) and isinstance(result.get("content"), list), "Missing MCP envelope"
    assert result.get("isError", False) is case["is_error"], "Unexpected MCP error status"
    serialized = json.dumps(result, ensure_ascii=False)
    assert token not in serialized, "Credential leaked into result"
    for text in ("Traceback (most recent call last)", "site-packages", "mongodb://", "mongodb+srv://"):
        assert text not in serialized, "Unsafe internal error detail"
    subset(result, case["result_subset"])
    for expected in case.get("expected_ids", []):
        assert pointer(result, expected["pointer"]) == expected["value"], "Expected fixture ID mismatch"


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Загружает только явно помеченные синтетические сценарии сервера."""
    assert path.stat().st_size <= 1_000_000, "Cases file too large"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document.get("synthetic_only") is True, "Cases must attest synthetic_only=true"
    cases = document["cases"]
    assert isinstance(cases, list) and 1 <= len(cases) <= 100, "Expected 1..100 cases"
    for case in cases:
        assert case["tool"] in TOOLS and case["kind"] in KINDS, "Unknown tool or kind"
        assert isinstance(case["args"], dict) and isinstance(case["is_error"], bool)
        assert isinstance(case["result_subset"], dict) and case["result_subset"], "Explicit expectation required"
        for expected in case.get("expected_ids", []):
            assert isinstance(expected["pointer"], str) and "value" in expected
    assert {case["kind"] for case in cases} == KINDS, "Need valid, empty, not_found, invalid_args cases"
    assert {case["tool"] for case in cases if case["kind"] == "valid"} == TOOLS, "Need valid case per tool"
    assert any(case["kind"] == "invalid_args" and case["is_error"] for case in cases)
    return cases


async def rejected_auth(url: str, token: str) -> None:
    """Проверяет отказ при отсутствии токена и при новом неверном токене."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "synthetic-harness", "version": "1"},
        },
    }
    wrong = secrets.token_urlsafe(32)
    assert wrong != token
    async with safe_client() as client:
        for authorization in (None, f"Bearer {wrong}"):
            headers = {"Accept": "application/json, text/event-stream"}
            if authorization is not None:
                headers["Authorization"] = authorization
            response = await client.post(url, headers=headers, json=payload)
            assert response.status_code in {401, 403}, "Missing/invalid machine auth accepted"
            assert token not in response.text and wrong not in response.text, "Auth response leaked credential"


async def filtered_discovery(url: str, token: str) -> None:
    """Проверяет настоящий tool_filter ADK в отдельном жизненном цикле."""
    client = toolset(url, token, filtered=True)
    try:
        async with asyncio.timeout(40):
            tools = await client.get_tools(readonly_context=None)
            assert [tool.name for tool in tools] == ["search_publications"], "Filtered discovery mismatch"
    finally:
        async with asyncio.timeout(10):
            await client.close()


async def run_cases(url: str, token: str, cases: list[dict[str, Any]]) -> int:
    """Выполняет обнаружение и вызовы в той же задаче, что закрывает клиент."""
    client = toolset(url, token)
    completed = 0
    try:
        async with asyncio.timeout(40):
            discovered = await client.get_tools(readonly_context=None)
        names = [tool.name for tool in discovered]
        assert len(names) == len(TOOLS) and set(names) == TOOLS, "Expected exact PAUK tool set"
        tools = {tool.name: tool for tool in discovered}
        tool_context = await context()
        for case in cases:
            async with asyncio.timeout(40):
                result = await tools[case["tool"]].run_async(args=case["args"], tool_context=tool_context)
            inspect_result(result, case, token)
            completed += 1
    finally:
        async with asyncio.timeout(10):
            await client.close()
    return completed


async def unreachable(url: str, token: str) -> None:
    """Проверяет ограниченный отказ на отдельно согласованном loopback-порту."""
    client = toolset(url, token)
    failed = False
    try:
        try:
            async with asyncio.timeout(12):
                await client.get_tools(readonly_context=None)
        except Exception:
            failed = True
        assert failed, "Optional endpoint unexpectedly reachable"
    finally:
        async with asyncio.timeout(10):
            await client.close()


async def execute(args: argparse.Namespace, cases: list[dict[str, Any]], token: str) -> dict[str, Any]:
    """Запускает только явно заданные локальные интеграционные проверки."""
    await rejected_auth(args.url, token)
    await filtered_discovery(args.url, token)
    completed = await run_cases(args.url, token, cases)
    if args.unreachable_url:
        assert args.unreachable_url != args.url, "Unreachable endpoint must differ"
        await unreachable(args.unreachable_url, token)
    return {
        "status": "passed",
        "actual_server_run": True,
        "cases_passed": completed,
        "machine_auth": "passed",
        "filtered_discovery": "passed",
        "unreachable": "passed" if args.unreachable_url else "not_requested",
    }


def parser() -> argparse.ArgumentParser:
    """Описывает CLI без аргументов для секретов."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", required=True, type=local_url)
    result.add_argument("--cases", required=True, type=Path)
    result.add_argument("--unreachable-url", type=local_url)
    return result


SUPPORTED_CLIENT_VERSIONS = frozenset({("2.3.0", "1.28.0"), ("2.12.0", "1.30.0")})


def main() -> int:
    """Публикует только обезличенный итог, без исключений и ответов сервера."""
    logging.basicConfig(level=logging.CRITICAL, format="%(message)s")
    LOGGER.setLevel(logging.INFO)
    args = parser().parse_args()
    if (version("google-adk"), version("mcp")) not in SUPPORTED_CLIENT_VERSIONS:
        LOGGER.error(
            '{"status":"blocked","reason":"Requires (google-adk 2.3.0, mcp 1.28.0) or (google-adk 2.12.0, mcp 1.30.0)"}'
        )
        return 2
    token = os.environ.get("PAUK_MCP_TEST_TOKEN", "")
    if len(token) < 24 or "\r" in token or "\n" in token:
        LOGGER.error('{"status":"blocked","reason":"Provide new synthetic PAUK_MCP_TEST_TOKEN (24+ characters)"}')
        return 2
    try:
        cases = load_cases(args.cases)
        report = asyncio.run(execute(args, cases, token))
    except (Exception, KeyboardInterrupt) as error:
        LOGGER.error(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "details": "suppressed to protect credentials and fixture data",
                }
            )
        )
        return 1
    LOGGER.info(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
