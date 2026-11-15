"""Public contracts and request-bound pagination."""

import base64
import hashlib
import hmac
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=True)]
DOI = Annotated[str, StringConstraints(min_length=1, max_length=512, strip_whitespace=True)]
TitleQuery = Annotated[str, StringConstraints(min_length=3, max_length=300, strip_whitespace=True)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=25)]
EvidenceSize = Annotated[int, Field(strict=True, ge=1, le=10)]
Status = Literal["available", "unavailable", "withheld"]
ErrorCode = Literal["INPUT", "CURSOR", "NOT_FOUND", "AMBIGUOUS_ID", "TIMEOUT", "BACKEND", "INTERNAL"]


class MCPError(Exception):
    """A public error containing only a fixed code."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code)


class Contract(BaseModel):
    """Reject undeclared fields and implicit scalar coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SearchRequest(Contract):
    """Exactly one bounded publication search."""

    doi: DOI | None = None
    title_contains: TitleQuery | None = None
    page_size: PageSize = 10
    cursor: Cursor | None = None

    @model_validator(mode="after")
    def one_query(self) -> "SearchRequest":
        """Require exactly one search mode."""
        if (self.doi is None) == (self.title_contains is None):
            raise ValueError("Exactly one query is required")
        if self.doi is not None and not normalize_doi(self.doi):
            raise ValueError("DOI cannot be empty")
        return self


class RepositoryRequest(Contract):
    """Canonical repository or historical alias lookup."""

    repository_id: Identifier


class PublicationRequest(Contract):
    """Bounded confirmed-repository page for one publication."""

    publication_id: Identifier
    page_size: PageSize = 10
    cursor: Cursor | None = None


class ExplainRequest(Contract):
    """Bounded evidence page for one publication/repository pair."""

    publication_id: Identifier
    repository_id: Identifier
    page_size: EvidenceSize = 5
    cursor: Cursor | None = None


class Text(Contract):
    """Bounded text with explicit disclosure and truncation status."""

    value: Annotated[str, Field(max_length=1000)] | None = None
    status: Status
    truncated: bool = False

    @model_validator(mode="after")
    def consistent_status(self) -> "Text":
        """Prevent withheld or unknown fields from carrying data."""
        if self.status == "available":
            if self.value is None or not self.value:
                raise ValueError("Available text requires a value")
        elif self.value is not None or self.truncated:
            raise ValueError("Unavailable text cannot carry a value")
        return self


class Publication(Contract):
    """Projected bibliographic fields, not a graph object."""

    publication_id: Identifier
    title: Text
    doi: DOI | None
    year: int | None


class RepositoryLink(Contract):
    """Canonical repository identity and display fields."""

    repository_id: Identifier
    url: Annotated[str, Field(max_length=2000)] | None
    name: Text


class Repository(RepositoryLink):
    """Nullable cached metadata, not a live repository inspection."""

    source_system: Literal["PAUK"] = "PAUK"
    retrieval_mode: Literal["cached_prepared_graph"] = "cached_prepared_graph"
    metadata_note: Literal[
        "Cached upstream metadata: last_updated is not an access check or evidence of current maintenance/abandonment; "
        "language is an unverified upstream label. Cache/access-check timestamps are not reported."
    ] = (
        "Cached upstream metadata: last_updated is not an access check or evidence of current maintenance/abandonment; "
        "language is an unverified upstream label. Cache/access-check timestamps are not reported."
    )
    verification_note: Literal[
        "This call did not check availability, inspect repository code or test reproducibility/completeness."
    ] = "This call did not check availability, inspect repository code or test reproducibility/completeness."

    description: Text
    language: Text
    license: Text
    archived: bool | None
    last_updated: Annotated[str, Field(max_length=100)] | None


class Evidence(Contract):
    """One stored extraction occurrence, not an independent source."""

    unit: Literal["extraction_occurrence"] = "extraction_occurrence"
    verdict: Literal["accepted", "rejected", "unknown"]
    classification_status: Text
    context: Text
    pages: Annotated[list[Annotated[int, Field(strict=True, ge=0)]], Field(max_length=20)]
    source_kind: Literal["abstract", "pdf_text", "pdf_annotation", "reference", "citation", "deposit_title"] | None
    source_url: Annotated[str, Field(max_length=2000)] | None = None
    confidence: Annotated[float, Field(allow_inf_nan=False)] | None
    reason: Text
    status: Status


class PublicationPage(Contract):
    """Stable-order publication continuation from the cached PAUK graph."""

    source_system: Literal["PAUK"] = "PAUK"
    retrieval_mode: Literal["cached_prepared_graph"] = "cached_prepared_graph"
    items: Annotated[list[Publication], Field(max_length=25)]
    next_cursor: Cursor | None


class RepositoryPage(Contract):
    """Only confirmed cached PAUK author-code links, with limited coverage."""

    source_system: Literal["PAUK"] = "PAUK"
    retrieval_mode: Literal["cached_prepared_graph"] = "cached_prepared_graph"
    publication_id: Identifier
    items: Annotated[list[RepositoryLink], Field(max_length=25)]
    next_cursor: Cursor | None
    coverage: Literal["Only confirmed PAUK links; empty means unknown, not absence of code globally."] = (
        "Only confirmed PAUK links; empty means unknown, not absence of code globally."
    )


class Explanation(Contract):
    """Separate cached implementation claims from extraction occurrences."""

    source_system: Literal["PAUK"] = "PAUK"
    retrieval_mode: Literal["cached_prepared_graph"] = "cached_prepared_graph"
    evidence_note: Literal[
        "Entries are extraction occurrences for this publication-repository pair, not independent sources. "
        "Document count and unique file/version identity are unknown; cached PDF IDs are logical references, "
        "not hashes, versions or URLs."
    ] = (
        "Entries are extraction occurrences for this publication-repository pair, not independent sources. "
        "Document count and unique file/version identity are unknown; cached PDF IDs are logical references, "
        "not hashes, versions or URLs."
    )

    publication_id: Identifier
    repository_id: Identifier
    implements: bool
    evidence: Annotated[list[Evidence], Field(max_length=10)]
    next_cursor: Cursor | None
    confidence_note: Literal["Classifier score is not calibrated; null means unknown."] = (
        "Classifier score is not calibrated; null means unknown."
    )


def normalize_doi(value: str) -> str:
    """Normalize only DOI wrappers and case, never perform substring matching."""
    normalized = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return normalized


def safe_url(value: object) -> str | None:
    """Withhold local, credential-bearing, signed or malformed URLs without rewriting them."""
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in ("https", "http")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return None
        if host not in {
            "github.com",
            "gitlab.com",
            "bitbucket.org",
            "codeberg.org",
            "sourceforge.net",
            "huggingface.co",
            "zenodo.org",
            "doi.org",
        } or parsed.port not in (None, 80, 443):
            return None
        return value
    except ValueError:
        return None


class Cursors:
    """Authenticate bounded JSON cursors and bind them to the normalized request."""

    def __init__(self, key: bytes) -> None:
        self.key = key

    def encode(self, request: dict[str, object], offset: int) -> str:
        """Create an opaque continuation without serializing Python objects."""
        payload = json.dumps([self._digest(request), offset], separators=(",", ":")).encode()
        signature = hmac.digest(self.key, payload, "sha256")
        return base64.urlsafe_b64encode(signature + payload).decode().rstrip("=")

    def decode(self, cursor: str | None, request: dict[str, object]) -> int:
        """Reject tampered, oversized and request-mismatched continuations."""
        if cursor is None:
            return 0
        try:
            if len(cursor) > 1024:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, payload, "sha256")):
                raise ValueError
            digest, offset = json.loads(payload)
            if digest != self._digest(request) or type(offset) is not int or not 0 <= offset <= 1_000_000:
                raise ValueError
            return offset
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise MCPError("CURSOR") from exc

    def _digest(self, request: dict[str, object]) -> str:
        return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
