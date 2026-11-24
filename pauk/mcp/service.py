"""Four bounded read operations over the published graph."""

import json
import math
from collections.abc import Callable
from typing import Any

from pauk.mcp import reader as queries
from pauk.mcp.contracts import (
    Contract,
    Cursors,
    Evidence,
    ExplainRequest,
    Explanation,
    MCPError,
    Publication,
    PublicationPage,
    PublicationRequest,
    Repository,
    RepositoryLink,
    RepositoryPage,
    RepositoryRequest,
    SearchRequest,
    Text,
    normalize_doi,
    safe_url,
)

SOURCES = {"abstract", "pdf_text", "pdf_annotation", "reference", "citation", "deposit_title"}
WIRE_RESPONSE_LIMIT = 65536
WIRE_ENVELOPE_HEADROOM = 8192
PAGE_WIRE_BUDGET = WIRE_RESPONSE_LIMIT - WIRE_ENVELOPE_HEADROOM


def _estimated_wire_bytes(page: Contract) -> int:
    """Estimate UTF-8 bytes of FastMCP's dual text and structuredContent JSON framing."""
    structured = page.model_dump_json()
    encoded_text = json.dumps(page.model_dump_json(indent=2), ensure_ascii=False)
    return len(structured.encode("utf-8")) + len(encoded_text.encode("utf-8"))


def text(value: object, *, allowed: bool = True, present: bool = False, limit: int = 1000) -> Text:
    """Represent unknown, withheld and truncated strings separately."""
    if not allowed:
        return Text(status="withheld" if present else "unavailable")
    if not isinstance(value, str) or not value:
        return Text(status="unavailable")
    return Text(value=value[:limit], status="available", truncated=len(value) > limit)


def _normalize_tokens(value: str | None) -> str | None:
    """Collapse internal whitespace and lowercase literal search tokens."""
    return " ".join(value.lower().split()) if value is not None else None


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or value.strip() != value:
        raise MCPError("BACKEND")
    return value


def _doi(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 512 else None


def _repository_link(row: dict[str, Any]) -> RepositoryLink:
    return RepositoryLink(
        repository_id=_identifier(row.get("id")), url=safe_url(row.get("url")), name=text(row.get("name"))
    )


def _evidence(row: dict[str, Any], allowed: bool) -> Evidence:
    aligned = row.get("aligned") is True
    raw_source = row.get("source")
    source = raw_source if aligned and isinstance(raw_source, str) and raw_source in SOURCES else None
    status = row.get("classification_status")
    status = status if isinstance(status, str) and status in {"pending", "classified", "failed"} else None
    page = row.get("page")
    pages = [page] if aligned and type(page) is int and (page > 0 or page == 0 and source == "abstract") else []
    confidence = row.get("confidence")
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        confidence = None
    relevant = row.get("relevant")
    return Evidence(
        verdict="accepted" if relevant is True else "rejected" if relevant is False else "unknown",
        classification_status=text(status, limit=100),
        context=text(row.get("context"), allowed=allowed and aligned, present=bool(row.get("context_present"))),
        pages=pages,
        source_kind=source,
        confidence=confidence,
        reason=text(row.get("reason"), allowed=allowed, present=bool(row.get("reason_present")), limit=500),
        status="unavailable" if not aligned else "available" if allowed else "withheld",
    )


class Service:
    """Expose only approved fields; private free text is disabled by default."""

    def __init__(self, reader: queries.Reader, cursor_key: bytes, *, safe_synthetic_text: bool = False) -> None:
        self.reader = reader
        self.cursors = Cursors(cursor_key)
        self.safe_synthetic_text = safe_synthetic_text

    def _page(self, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        cursor = request.pop("cursor")
        page_size = request.pop("page_size")
        offset = self.cursors.decode(cursor, request)
        return request, {"offset": offset, "limit": page_size + 1}

    def _next(self, binding: dict[str, Any], parameters: dict[str, Any], rows: list[Any], emitted: int) -> str | None:
        return self.cursors.encode(binding, parameters["offset"] + emitted) if len(rows) > emitted else None

    def _fit_page[TPage: Contract, TItem](
        self,
        binding: dict[str, Any],
        parameters: dict[str, Any],
        rows: list[Any],
        candidates: list[TItem],
        build: Callable[[list[TItem], str | None], TPage],
    ) -> TPage:
        """Shrink a page to the largest prefix that fits inside the safe wire budget."""
        page = build(candidates, self._next(binding, parameters, rows, len(candidates)))
        if len(candidates) <= 1 or _estimated_wire_bytes(page) <= PAGE_WIRE_BUDGET:
            return page
        low, high = 1, len(candidates) - 1
        best = build(candidates[:1], self._next(binding, parameters, rows, 1))
        while low <= high:
            mid = (low + high) // 2
            candidate = build(candidates[:mid], self._next(binding, parameters, rows, mid))
            if _estimated_wire_bytes(candidate) <= PAGE_WIRE_BUDGET:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    async def search_publications(self, request: SearchRequest) -> PublicationPage:
        """Search by exact normalized DOI, literal title tokens, or confirmed repository name/URL."""
        normalized = request.model_dump()
        normalized["doi"] = normalize_doi(request.doi) if request.doi is not None else None
        normalized["title_contains"] = _normalize_tokens(request.title_contains)
        normalized["repository_contains"] = _normalize_tokens(request.repository_contains)
        binding, parameters = self._page({"tool": "search", **normalized})
        if request.doi is not None:
            parameters["query"] = binding["doi"]
            query = queries.SEARCH_DOI
        elif request.title_contains is not None:
            parameters["query"] = binding["title_contains"]
            query = queries.SEARCH_TITLE
        else:
            parameters["query"] = binding["repository_contains"]
            query = queries.SEARCH_REPOSITORY
        rows = await self.reader.query(query, parameters)
        candidates = [
            Publication(
                publication_id=_identifier(row.get("id")),
                title=text(row.get("title")),
                doi=_doi(row.get("doi")),
                year=row.get("year") if type(row.get("year")) is int else None,
            )
            for row in rows[: request.page_size]
        ]
        return self._fit_page(
            binding,
            parameters,
            rows,
            candidates,
            lambda page_items, next_cursor: PublicationPage(items=page_items, next_cursor=next_cursor),
        )

    async def get_publication_repositories(self, request: PublicationRequest) -> RepositoryPage:
        """Return only canonical repositories linked by IMPLEMENTS, not mentions."""
        binding, parameters = self._page({"tool": "repositories", **request.model_dump()})
        publication_id = _identifier(await queries.resolve(self.reader, "Publication", request.publication_id))
        rows = await self.reader.query(queries.REPOSITORIES, {**parameters, "publication_id": publication_id})
        candidates = [_repository_link(r) for r in rows[: request.page_size]]
        return self._fit_page(
            binding,
            parameters,
            rows,
            candidates,
            lambda page_items, next_cursor: RepositoryPage(
                publication_id=publication_id,
                items=page_items,
                next_cursor=next_cursor,
            ),
        )

    async def get_repository(self, request: RepositoryRequest) -> Repository:
        """Read bounded repository metadata, withholding its description by default."""
        repository_id = _identifier(await queries.resolve(self.reader, "Repository", request.repository_id))
        rows = await self.reader.query(
            queries.REPOSITORY,
            {
                "repository_id": repository_id,
                "text": self.safe_synthetic_text,
            },
        )
        if len(rows) != 1:
            raise MCPError("BACKEND")
        row = rows[0]
        updated = row.get("last_updated")
        return Repository(
            **_repository_link(row).model_dump(),
            description=text(
                row.get("description"), allowed=self.safe_synthetic_text, present=bool(row.get("description_present"))
            ),
            language=text(row.get("language")),
            license=text(row.get("license")),
            archived=row.get("archived") if type(row.get("archived")) is bool else None,
            last_updated=updated if isinstance(updated, str) and len(updated) <= 100 else None,
        )

    async def explain_link(self, request: ExplainRequest) -> Explanation:
        """Keep author-code claims separate from bounded, genuinely stored mention evidence."""
        binding, parameters = self._page({"tool": "explain", **request.model_dump()})
        publication_id = _identifier(await queries.resolve(self.reader, "Publication", request.publication_id))
        repository_id = _identifier(await queries.resolve(self.reader, "Repository", request.repository_id))
        pair = {"publication_id": publication_id, "repository_id": repository_id}
        implements = bool(await self.reader.query(queries.IMPLEMENTS, pair))
        rows = await self.reader.query(queries.EVIDENCE, {**pair, **parameters, "text": self.safe_synthetic_text})
        candidates = [_evidence(r, self.safe_synthetic_text) for r in rows[: request.page_size]]
        return self._fit_page(
            binding,
            parameters,
            rows,
            candidates,
            lambda page_items, next_cursor: Explanation(
                **pair,
                implements=implements,
                evidence=page_items,
                next_cursor=next_cursor,
            ),
        )
