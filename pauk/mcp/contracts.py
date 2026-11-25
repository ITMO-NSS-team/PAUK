"""Public contracts and request-bound pagination."""

import base64
import hashlib
import hmac
import ipaddress
import json
import socket
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pauk.urls import (
    APPROVED_REPO_HOSTS as APPROVED_REPO_HOSTS,
)
from pauk.urls import (
    normalize_repo_url_query as normalize_repo_url_query,
)
from pauk.urls import (
    safe_url as safe_url,
)

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=True)]
DOI = Annotated[str, StringConstraints(min_length=1, max_length=512, strip_whitespace=True)]
TitleQuery = Annotated[str, StringConstraints(min_length=3, max_length=300, strip_whitespace=True)]
RepoURLQuery = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
RepositoryRelation = Literal["implements", "mentions"]
ProposedRelation = Literal["implements", "not_implements", "mentions", "full_text"]
SubmissionText = Annotated[str, StringConstraints(min_length=10, max_length=1000, strip_whitespace=True)]
SubmissionQuote = Annotated[str, StringConstraints(min_length=1, max_length=1000, strip_whitespace=True)]
EvidenceURL = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=25)]
EvidenceSize = Annotated[int, Field(strict=True, ge=1, le=10)]
Status = Literal["available", "unavailable", "withheld"]
ErrorCode = Literal["INPUT", "CURSOR", "NOT_FOUND", "AMBIGUOUS_ID", "QUEUE_FULL", "TIMEOUT", "BACKEND", "INTERNAL"]


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
    repository_contains: TitleQuery | None = None
    page_size: PageSize = 10
    cursor: Cursor | None = None

    @model_validator(mode="after")
    def one_query(self) -> "SearchRequest":
        """Require exactly one search mode."""
        provided = sum(value is not None for value in (self.doi, self.title_contains, self.repository_contains))
        if provided != 1:
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


class SearchRepositoriesRequest(Contract):
    """Exactly one bounded repository lookup by normalized URL or text tokens."""

    url: RepoURLQuery | None = None
    query: TitleQuery | None = None
    page_size: PageSize = 10
    cursor: Cursor | None = None

    @model_validator(mode="after")
    def one_query(self) -> "SearchRepositoriesRequest":
        """Require exactly one repository search mode and a valid approved URL when given."""
        provided = sum(value is not None for value in (self.url, self.query))
        if provided != 1:
            raise ValueError("Exactly one query is required")
        if self.url is not None and normalize_repo_url_query(self.url) is None:
            raise ValueError("Repository URL must use an approved host without credentials or query parameters")
        return self


class RepositoryPublicationsRequest(Contract):
    """Bounded publications page for one repository (authored IMPLEMENTS or MENTIONS_LINK)."""

    repository_id: Identifier
    relation: RepositoryRelation = "implements"
    page_size: PageSize = 10
    cursor: Cursor | None = None


def _has_disallowed_control_chars(value: str) -> bool:
    """Reject C0 controls (except tab, LF, CR), DEL and C1 controls."""
    return any((ord(ch) < 32 and ch not in "\t\n\r") or 0x7F <= ord(ch) <= 0x9F for ch in value)


class SubmitLinkEvidenceRequest(Contract):
    """Queue candidate repository/full-text evidence for PAUK human moderation."""

    publication_id: Identifier
    proposed_relation: ProposedRelation
    rationale: SubmissionText
    repository_id: Identifier | None = None
    repository_url: RepoURLQuery | None = None
    quote: SubmissionQuote | None = None
    evidence_url: EvidenceURL | None = None

    @model_validator(mode="after")
    def validate_target_and_text(self) -> "SubmitLinkEvidenceRequest":
        """Enforce target requirements, safe URLs and safe control characters."""
        if _has_disallowed_control_chars(self.rationale) or (
            self.quote is not None and _has_disallowed_control_chars(self.quote)
        ):
            raise ValueError("Text fields cannot contain control characters")
        if self.repository_url is not None and normalize_repo_url_query(self.repository_url) is None:
            raise ValueError("Repository URL must use an approved host without credentials or query parameters")
        if self.evidence_url is not None and safe_evidence_url(self.evidence_url) is None:
            raise ValueError("Evidence URL must use a public http(s) hostname without credentials")
        if self.proposed_relation == "full_text":
            if self.evidence_url is None or self.repository_id is not None or self.repository_url is not None:
                raise ValueError("full_text suggestions require evidence_url and forbid repository fields")
        elif self.repository_id is None and self.repository_url is None:
            raise ValueError("Repository link suggestions require repository_id or repository_url")
        return self


class LinkSuggestionReceipt(Contract):
    """Safe confirmation returned to agents after queuing evidence for human review."""

    source_system: Literal["PAUK"] = "PAUK"
    retrieval_mode: Literal["cached_prepared_graph"] = "cached_prepared_graph"
    suggestion_id: str
    status: Literal["queued", "already_queued", "already_reviewed"]
    publication_id: str
    proposed_relation: ProposedRelation
    repository_id: str | None = None
    repository_url: str | None = None
    review_note: str = "Queued for manual PAUK human review; the published graph was not modified."


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


class RepositorySearchPage(Contract):
    """Stable-order repository continuation from the cached PAUK graph."""

    source_system: Literal["PAUK"] = "PAUK"
    retrieval_mode: Literal["cached_prepared_graph"] = "cached_prepared_graph"
    items: Annotated[list[RepositoryLink], Field(max_length=25)]
    next_cursor: Cursor | None


class RepositoryPublication(Publication):
    """Publication linked to a repository, separating confirmed code from mentions/reuse."""

    implements: bool
    verdict: Literal["accepted", "rejected", "unknown"] | None = None


class RepositoryPublicationsPage(Contract):
    """Publications linked to one repository as authored code or mentions/reuse."""

    source_system: Literal["PAUK"] = "PAUK"
    retrieval_mode: Literal["cached_prepared_graph"] = "cached_prepared_graph"
    repository_id: Identifier
    relation: RepositoryRelation
    items: Annotated[list[RepositoryPublication], Field(max_length=25)]
    next_cursor: Cursor | None
    coverage: Literal[
        "Cached PAUK links only: relation='implements' returns confirmed author-code links; "
        "relation='mentions' includes rejected and unclassified mentions. "
        "Empty means unknown in PAUK, not absence of related publications globally."
    ] = (
        "Cached PAUK links only: relation='implements' returns confirmed author-code links; "
        "relation='mentions' includes rejected and unclassified mentions. "
        "Empty means unknown in PAUK, not absence of related publications globally."
    )


def normalize_doi(value: str) -> str:
    """Normalize only DOI wrappers and case, never perform substring matching."""
    normalized = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return normalized


_EVIDENCE_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")
_RESERVED_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".localdomain",
    ".internal",
    ".lan",
    ".arpa",
    ".invalid",
    ".test",
    ".example",
    ".home",
    ".corp",
    ".intranet",
    ".private",
)


def _valid_public_tld(tld: str) -> bool:
    return (tld.isalpha() and len(tld) >= 2) or (
        tld.startswith("xn--") and len(tld) > 4 and tld[4:].replace("-", "").isalnum()
    )


def safe_evidence_url(value: object) -> str | None:
    """Validate a public http(s) evidence/PDF URL without accepting local hosts, IPs or credentials."""
    if not isinstance(value, str):
        return None
    stripped = value.strip(" \t\n\r")
    if (
        not stripped
        or len(stripped) > 1000
        or any(ord(ch) < 33 or 0x7F <= ord(ch) <= 0x9F for ch in stripped)
        or "@" in stripped
    ):
        return None
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return None
    if (
        parsed.scheme not in ("https", "http")
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.netloc.isascii()
        or parsed.netloc.endswith(":")
    ):
        return None
    default_port = ":443" if parsed.scheme == "https" else ":80"
    host = parsed.netloc.lower().removesuffix(default_port)
    if (
        not host
        or ":" in host
        or host.startswith(".")
        or host.endswith(".")
        or ".." in host
        or "." not in host
        or not parsed.path.strip("/")
        or any(ch not in _EVIDENCE_HOST_CHARS for ch in host)
        or any(not label or label.startswith("-") or label.endswith("-") for label in host.split("."))
        or host.endswith(_RESERVED_HOST_SUFFIXES)
        or not _valid_public_tld(host.rsplit(".", 1)[-1])
    ):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    try:
        socket.inet_aton(host)
    except OSError:
        pass
    else:
        return None
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


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
