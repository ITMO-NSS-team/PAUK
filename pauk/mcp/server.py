"""Local Streamable HTTP transport with a machine-token boundary."""

import asyncio
import hmac
import json
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ClientNotification, ClientRequest
from neo4j import AsyncGraphDatabase
from pydantic import ValidationError
from pymongo import MongoClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pauk.mcp.contracts import (
    DOI,
    Contract,
    Cursor,
    EvidenceSize,
    EvidenceURL,
    ExplainRequest,
    Explanation,
    Identifier,
    LinkSuggestionReceipt,
    MCPError,
    PageSize,
    ProposedRelation,
    PublicationPage,
    PublicationRequest,
    Repository,
    RepositoryPage,
    RepositoryPublicationsPage,
    RepositoryPublicationsRequest,
    RepositoryRelation,
    RepositoryRequest,
    RepositorySearchPage,
    RepoURLQuery,
    SearchRepositoriesRequest,
    SearchRequest,
    SubmissionQuote,
    SubmissionText,
    SubmitLinkEvidenceRequest,
    TitleQuery,
)
from pauk.mcp.reader import Neo4jReader
from pauk.mcp.service import Service
from pauk.mcp.submissions import MongoSuggestionStore

if TYPE_CHECKING:
    from pauk.settings import Settings

REQUESTS: dict[str, type[Contract]] = {
    "search_publications": SearchRequest,
    "get_publication_repositories": PublicationRequest,
    "get_repository": RepositoryRequest,
    "explain_link": ExplainRequest,
    "search_repositories": SearchRepositoriesRequest,
    "get_repository_publications": RepositoryPublicationsRequest,
}
SUBMISSION_REQUESTS: dict[str, type[Contract]] = {
    "submit_link_evidence": SubmitLinkEvidenceRequest,
}


def _allowed_requests(service: Service) -> dict[str, type[Contract]]:
    """Return active request contracts, adding submission tools only when enabled."""
    if service.submissions_enabled:
        return {**REQUESTS, **SUBMISSION_REQUESTS}
    return dict(REQUESTS)


class SafeMCP(FastMCP):
    """Sanitize SDK argument/output errors as well as backend failures."""

    def __init__(self, service: Service, *, tool_timeout: float = 5.0, **kwargs: Any) -> None:
        self.service = service
        self.allowed_requests = _allowed_requests(service)
        self.tool_timeout = tool_timeout
        super().__init__(**kwargs)

    async def _dispatch(self, name: str, request: Contract) -> Any:
        if name == "search_publications" and isinstance(request, SearchRequest):
            return await self.service.search_publications(request)
        if name == "get_publication_repositories" and isinstance(request, PublicationRequest):
            return await self.service.get_publication_repositories(request)
        if name == "get_repository" and isinstance(request, RepositoryRequest):
            return await self.service.get_repository(request)
        if name == "explain_link" and isinstance(request, ExplainRequest):
            return await self.service.explain_link(request)
        if name == "search_repositories" and isinstance(request, SearchRepositoriesRequest):
            return await self.service.search_repositories(request)
        if name == "get_repository_publications" and isinstance(request, RepositoryPublicationsRequest):
            return await self.service.get_repository_publications(request)
        if name == "submit_link_evidence" and isinstance(request, SubmitLinkEvidenceRequest):
            return await self.service.submit_link_evidence(request)
        raise ToolError("INPUT")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Validate strict DTOs without SDK JSON re-parsing and apply a cancellable deadline."""
        if name not in self.allowed_requests:
            raise ToolError("INPUT")
        try:
            validated = self.allowed_requests[name].model_validate(arguments)
        except ValidationError:
            raise ToolError("INPUT") from None
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise ToolError("INPUT")
        try:
            async with asyncio.timeout(self.tool_timeout):
                result = await self._dispatch(name, validated)
                return tool.fn_metadata.convert_result(result)
        except TimeoutError:
            raise ToolError("TIMEOUT") from None
        except Exception as exc:
            cause: BaseException | None = exc
            for _ in range(8):
                if isinstance(cause, MCPError):
                    raise ToolError(cause.code) from None
                cause = cause.__cause__ if cause is not None else None
            raise ToolError("INTERNAL") from None


def register_tools(mcp: FastMCP, service: Service) -> None:
    """Register the read tools plus optional opt-in submission tools."""
    _register_publication_tools(mcp, service)
    _register_repository_tools(mcp, service)
    if service.submissions_enabled:
        _register_submission_tools(mcp, service)


def _register_publication_tools(mcp: FastMCP, service: Service) -> None:

    @mcp.tool(structured_output=True)
    async def search_publications(
        doi: DOI | None = None,
        title_contains: TitleQuery | None = None,
        repository_contains: TitleQuery | None = None,
        page_size: PageSize = 10,
        cursor: Cursor | None = None,
    ) -> PublicationPage:
        """Find publications by exactly one of normalized DOI, literal title fragment, or confirmed repository name/URL."""
        return await service.search_publications(
            SearchRequest(
                doi=doi,
                title_contains=title_contains,
                repository_contains=repository_contains,
                page_size=page_size,
                cursor=cursor,
            )
        )

    @mcp.tool(structured_output=True)
    async def get_publication_repositories(
        publication_id: Identifier,
        page_size: PageSize = 10,
        cursor: Cursor | None = None,
    ) -> RepositoryPage:
        """Get IMPLEMENTS author-code links only; empty means unknown in PAUK."""
        return await service.get_publication_repositories(
            PublicationRequest(
                publication_id=publication_id,
                page_size=page_size,
                cursor=cursor,
            )
        )


def _register_repository_tools(mcp: FastMCP, service: Service) -> None:
    @mcp.tool(structured_output=True)
    async def get_repository(repository_id: Identifier) -> Repository:
        """Get projected metadata; private description is withheld unless explicitly approved."""
        return await service.get_repository(RepositoryRequest(repository_id=repository_id))

    @mcp.tool(structured_output=True)
    async def explain_link(
        publication_id: Identifier,
        repository_id: Identifier,
        page_size: EvidenceSize = 5,
        cursor: Cursor | None = None,
    ) -> Explanation:
        """Explain IMPLEMENTS separately from stored mentions, without inferred PDF provenance."""
        return await service.explain_link(
            ExplainRequest(
                publication_id=publication_id,
                repository_id=repository_id,
                page_size=page_size,
                cursor=cursor,
            )
        )

    @mcp.tool(structured_output=True)
    async def search_repositories(
        url: RepoURLQuery | None = None,
        query: TitleQuery | None = None,
        page_size: PageSize = 10,
        cursor: Cursor | None = None,
    ) -> RepositorySearchPage:
        """Find repositories by normalized approved URL or literal name/URL tokens."""
        return await service.search_repositories(
            SearchRepositoriesRequest(url=url, query=query, page_size=page_size, cursor=cursor)
        )

    @mcp.tool(structured_output=True)
    async def get_repository_publications(
        repository_id: Identifier,
        relation: RepositoryRelation = "implements",
        page_size: PageSize = 10,
        cursor: Cursor | None = None,
    ) -> RepositoryPublicationsPage:
        """List publications implemented by a repository or mentioning/reusing it."""
        return await service.get_repository_publications(
            RepositoryPublicationsRequest(
                repository_id=repository_id,
                relation=relation,
                page_size=page_size,
                cursor=cursor,
            )
        )


def _register_submission_tools(mcp: FastMCP, service: Service) -> None:
    @mcp.tool(structured_output=True)
    async def submit_link_evidence(
        publication_id: Identifier,
        proposed_relation: ProposedRelation,
        rationale: SubmissionText,
        repository_id: Identifier | None = None,
        repository_url: RepoURLQuery | None = None,
        quote: SubmissionQuote | None = None,
        evidence_url: EvidenceURL | None = None,
    ) -> LinkSuggestionReceipt:
        """Queue candidate repository or full-text evidence for manual PAUK human review."""
        return await service.submit_link_evidence(
            SubmitLinkEvidenceRequest(
                publication_id=publication_id,
                proposed_relation=proposed_relation,
                rationale=rationale,
                repository_id=repository_id,
                repository_url=repository_url,
                quote=quote,
                evidence_url=evidence_url,
            )
        )


class ResponseLimit(Exception):
    """Stop an oversized response before sending any bytes."""


def _input_response(identifier: str | int | None = None, *, tool: bool = False) -> JSONResponse:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
    if tool:
        payload["result"] = {"content": [{"type": "text", "text": "INPUT"}], "isError": True}
    else:
        payload["error"] = {"code": -32600, "message": "INPUT"}
    return JSONResponse(payload, status_code=200 if tool else 400)


def _reject_constant(_: str) -> None:
    raise ValueError("Non-finite JSON number")


def _tool_input_error(
    message: dict[str, Any], allowed_requests: dict[str, type[Contract]] = REQUESTS
) -> JSONResponse | None:
    params = message.get("params", {})
    name, arguments = params.get("name"), params.get("arguments", {})
    if name not in allowed_requests or not isinstance(arguments, dict):
        return _input_response(message["id"], tool=True)
    try:
        allowed_requests[name].model_validate(arguments)
    except ValidationError:
        return _input_response(message["id"], tool=True)
    return None


def _envelope_error(
    body: bytes, allowed_requests: dict[str, type[Contract]] = REQUESTS
) -> JSONResponse | None:
    try:
        message = json.loads(body, parse_constant=_reject_constant)
        if not isinstance(message, dict) or set(message) - {"jsonrpc", "id", "method", "params"}:
            return _input_response()
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _input_response()
        if "params" in message and not isinstance(message["params"], dict):
            return _input_response()
        if "id" not in message:
            if method not in {"notifications/initialized", "notifications/cancelled", "notifications/progress"}:
                return _input_response()
            ClientNotification.model_validate(message, strict=True)
            return None
        identifier = message["id"]
        if type(identifier) not in (str, int):
            return _input_response()
        if method not in {"initialize", "ping", "tools/list", "tools/call"}:
            return _input_response(identifier)
        ClientRequest.model_validate(message, strict=True)
        return _tool_input_error(message, allowed_requests) if method == "tools/call" else None
    except (ValueError, TypeError, RecursionError):
        return _input_response()


def _authorization(token: str) -> bytes:
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("MCP token requires at least 32 non-whitespace ASCII characters")
    return ("Bearer " + token).encode()


class Boundary:
    """Authenticate before body access and buffer bounded JSON protocol responses."""

    def __init__(
        self, app: ASGIApp, token: str, *, allowed_requests: dict[str, type[Contract]] | None = None
    ) -> None:
        self.app = app
        self.authorization = _authorization(token)
        self.allowed_requests = allowed_requests or REQUESTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        if len(auth) != 1 or not hmac.compare_digest(auth[0], self.authorization):
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        if scope["method"] == "GET":
            await Response(status_code=405)(scope, receive, send)
            return
        try:
            async with asyncio.timeout(5):
                body = await self._body(receive)
        except (ResponseLimit, TimeoutError):
            await Response(status_code=413)(scope, receive, send)
            return
        error = _envelope_error(body, self.allowed_requests)
        if error is not None:
            await error(scope, receive, send)
            return
        await self._respond(scope, body, receive, send)

    async def _body(self, receive: Receive) -> bytes:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise ResponseLimit
            body.extend(message.get("body", b""))
            if len(body) > 8192:
                raise ResponseLimit
            if not message.get("more_body", False):
                return bytes(body)

    async def _respond(self, scope: Scope, body: bytes, receive: Receive, send: Send) -> None:
        messages: list[Message] = []
        size = 0
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body}
            return await receive()

        async def collect(message: Message) -> None:
            nonlocal size
            size += len(message.get("body", b""))
            if size <= 65536:
                messages.append(message)

        try:
            await self.app(scope, replay, collect)
        except Exception:
            await JSONResponse({"error": "INTERNAL"}, status_code=500)(scope, receive, send)
            return
        if size > 65536:
            await JSONResponse({"error": "INTERNAL"}, status_code=500)(scope, receive, send)
            return
        for message in messages:
            await send(message)


AppLifespan = Callable[[Starlette], AbstractAsyncContextManager[None]]


def _own_app_resources(app: Starlette, lifespan: AppLifespan) -> None:
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def application_lifespan(application: Starlette) -> AsyncIterator[None]:
        async with lifespan(application), sdk_lifespan(application):
            yield

    app.router.lifespan_context = application_lifespan


def create_app(
    service: Service, token: str, *, tool_timeout: float = 5.0, lifespan: AppLifespan | None = None
) -> Boundary:
    """Run optional resources once per ASGI application, preserving the SDK lifespan."""
    mcp = SafeMCP(
        service,
        name="PAUK isolated read prototype",
        host="127.0.0.1",
        tool_timeout=tool_timeout,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )
    register_tools(mcp, service)
    app = mcp.streamable_http_app()
    if lifespan is not None:
        _own_app_resources(app, lifespan)
    return Boundary(app, token, allowed_requests=_allowed_requests(service))


def _has_unsafe_uri_chars(value: str) -> bool:
    return any(ord(ch) < 33 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def _check_configuration(settings: "Settings") -> None:
    try:
        uri = urlsplit(settings.mcp_neo4j_uri)
        valid = (
            settings.mcp_dataset_policy in ("isolated", "safe_synthetic")
            and not _has_unsafe_uri_chars(settings.mcp_neo4j_uri)
            and "@" not in settings.mcp_neo4j_uri
            and uri.hostname in ("localhost", "127.0.0.1")
            and uri.scheme == "bolt"
            and uri.port is not None
            and uri.netloc.endswith(f":{uri.port}")
            and 1 <= uri.port <= 65535
            and uri.port != 7687
            and uri.username is None
            and uri.password is None
            and not uri.path
            and not uri.query
            and not uri.fragment
            and bool(settings.mcp_neo4j_user and settings.mcp_neo4j_password and settings.mcp_neo4j_database)
            and 1 <= settings.mcp_port <= 65535
        )
    except ValueError:
        valid = False
    if not valid:
        raise SystemExit("MCP requires explicit isolated loopback graph configuration on a non-default port")
    if settings.mcp_submissions_enabled:
        db_name = settings.mcp_mongo_db
        try:
            mongo_uri = urlsplit(settings.mcp_mongo_uri)
            mongo_valid = (
                not _has_unsafe_uri_chars(settings.mcp_mongo_uri)
                and "@" not in settings.mcp_mongo_uri
                and mongo_uri.scheme == "mongodb"
                and mongo_uri.hostname in ("localhost", "127.0.0.1")
                and mongo_uri.port is not None
                and mongo_uri.netloc.endswith(f":{mongo_uri.port}")
                and 1 <= mongo_uri.port <= 65535
                and mongo_uri.port != 27017
                and mongo_uri.username is None
                and mongo_uri.password is None
                and not mongo_uri.path
                and not mongo_uri.query
                and not mongo_uri.fragment
                and bool(db_name)
                and db_name == db_name.strip()
                and not any(
                    ch in '/\\. "$*<>:|?' or ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in db_name
                )
            )
        except ValueError:
            mongo_valid = False
        if not mongo_valid:
            raise SystemExit(
                "MCP submissions require explicit isolated loopback Mongo configuration on a non-default port"
            )
    _authorization(settings.mcp_token)


def main() -> None:
    """Serve only an explicitly configured isolated graph, never generic PAUK credentials."""
    import uvicorn

    from pauk.settings import settings

    _check_configuration(settings)
    driver = AsyncGraphDatabase.driver(
        settings.mcp_neo4j_uri,
        auth=(settings.mcp_neo4j_user, settings.mcp_neo4j_password),
        connection_timeout=3.0,
        max_transaction_retry_time=0.0,
    )
    mongo_client = (
        MongoClient(
            settings.mcp_mongo_uri,
            serverSelectionTimeoutMS=2000,
            socketTimeoutMS=3000,
            connectTimeoutMS=2000,
        )
        if settings.mcp_submissions_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await driver.close()
            if mongo_client is not None:
                mongo_client.close()

    service = Service(
        Neo4jReader(driver, settings.mcp_neo4j_database),
        secrets.token_bytes(32),
        safe_synthetic_text=settings.mcp_dataset_policy == "safe_synthetic",
        suggestion_store=(
            MongoSuggestionStore(mongo_client[settings.mcp_mongo_db]) if mongo_client is not None else None
        ),
    )
    uvicorn.run(create_app(service, settings.mcp_token, lifespan=lifespan), host="127.0.0.1", port=settings.mcp_port)
