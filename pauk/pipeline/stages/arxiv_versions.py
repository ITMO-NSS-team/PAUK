"""Append an arXiv PDF link to papers without full text, found by DOI via Semantic Scholar.

Runs before code_links, which reads each newly discovered source independently
of the publication's main PDF and existing repository evidence.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pauk.models import Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.models.publication import ArxivSource
from pauk.redaction import redact_text
from pauk.sources.base import HttpRequestError
from pauk.sources.semantic_scholar import MAX_BATCH, SemanticScholarClient, arxiv_id

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")
_ARXIV_ID = re.compile(r"(?P<base>\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})(?P<revision>v[1-9]\d*)?")
_ARXIV_DOI = "10.48550/arxiv."


@dataclass(frozen=True)
class ArxivPreprint:
    """A prepared record with intrinsic arXiv identity and a possibly reusable cache."""

    publication_id: str
    cache_key: str
    base: str
    revision: str
    cache_compatible: bool


def bare_doi(doi: str) -> str:
    value = doi.strip()
    for prefix in DOI_PREFIXES:
        if value.lower().startswith(prefix):
            return value[len(prefix):]
    return value


def _parse_arxiv_id(identifier: str) -> tuple[str, str] | None:
    """Return the work ID and optional revision of a valid modern or legacy ID."""
    match = _ARXIV_ID.fullmatch(identifier.casefold())
    return (match["base"], match["revision"] or "") if match else None


def arxiv_pdf_identity(url: str, *, pdf_only: bool = False) -> tuple[str, str] | None:
    """Parse an ID only from a recognized arXiv PDF or abstract URL."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ARXIV_HOSTS:
        return None
    route, _, name = parsed.path.lstrip("/").partition("/")
    if route != "pdf" and (pdf_only or route != "abs"):
        return None
    return _parse_arxiv_id(name.removesuffix(".pdf"))


def arxiv_doi_identity(doi: str) -> tuple[str, str] | None:
    """Identify a native arXiv DOI, including an explicitly listed revision."""
    doi = bare_doi(doi).casefold()
    return _parse_arxiv_id(doi[len(_ARXIV_DOI):]) if doi.startswith(_ARXIV_DOI) else None


def native_arxiv_pdf_url(publication: Publication) -> str | None:
    """Supply a primary PDF candidate when a native arXiv DOI lists none."""
    identity = arxiv_doi_identity(publication.doi or "") if not publication.pdf_urls else None
    return f"https://arxiv.org/pdf/{''.join(identity)}" if identity else None


def _extract_preprint_candidate(
    *,
    publication_id: str,
    cache_key: str,
    record_type: str | None,
    doi: str | None,
    pdf_urls: Iterable[str],
    has_supplemental: bool,
    block_legacy_merged: bool,
) -> ArxivPreprint | None:
    """Use intrinsic preprint metadata; aggregated or mixed PDFs cannot establish cache provenance."""
    doi_id = arxiv_doi_identity(doi or "")
    pdf_ids = {arxiv_pdf_identity(url) for url in pdf_urls}
    recognized = {identity for identity in pdf_ids if identity is not None}
    preprint_only = record_type == "preprint" or (record_type is None and not doi)
    if not doi_id and (not preprint_only or has_supplemental or not recognized):
        return None
    bases = {base for base, _ in recognized}
    if not doi_id and len(bases) != 1:
        return None
    base, doi_revision = doi_id or (next(iter(bases)), "")
    revisions = {revision for pdf_base, revision in recognized if pdf_base == base}
    revision = doi_revision or (next(iter(revisions)) if len(revisions) == 1 else "")
    cache_compatible = (
        not has_supplemental and not block_legacy_merged
        and (not pdf_ids or pdf_ids == {(base, revision)})
    )
    return ArxivPreprint(publication_id, cache_key, base, revision, cache_compatible)


def _preprint_identities(publication: Publication) -> list[ArxivPreprint]:
    """Identify per-record (or legacy publication-level) arXiv preprint caches."""
    supplemental = {source.url for source in publication.arxiv_sources}
    if publication.source_records:
        single_record = len(publication.source_records) == 1
        candidates: list[ArxivPreprint] = []
        for record in publication.source_records:
            record_type = record.type if (not single_record or record.type is not None) else publication.type
            record_doi = record.doi if (not single_record or record.doi is not None) else publication.doi
            raw_urls = record.pdf_urls if (not single_record or record.pdf_urls) else publication.pdf_urls
            candidate = _extract_preprint_candidate(
                publication_id=publication.id,
                cache_key=record.key,
                record_type=record_type,
                doi=record_doi,
                pdf_urls=(url for url in raw_urls if url not in supplemental),
                has_supplemental=bool(supplemental),
                block_legacy_merged=False,
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates
    legacy = _extract_preprint_candidate(
        publication_id=publication.id,
        cache_key=publication.id,
        record_type=publication.type,
        doi=publication.doi,
        pdf_urls=(url for url in publication.pdf_urls if url not in supplemental),
        has_supplemental=bool(supplemental),
        block_legacy_merged=bool(publication.merged_ids),
    )
    return [legacy] if legacy is not None else []


def arxiv_preprints(publications: Iterable[Publication]) -> dict[str, list[ArxivPreprint]]:
    """Index prepared preprints by arXiv work ID, keeping duplicates visible."""
    indexed: dict[str, list[ArxivPreprint]] = defaultdict(list)
    for publication in publications:
        for preprint in _preprint_identities(publication):
            indexed[preprint.base].append(preprint)
    return indexed


def has_arxiv_link(publication: Publication) -> bool:
    return any(arxiv_pdf_identity(url) is not None for url in publication.pdf_urls)


def wants_arxiv_copy(publication: Publication) -> bool:
    return (bool(publication.doi) and not (publication.full_text or "").strip()
            and not arxiv_doi_identity(publication.doi or "")
            and not has_arxiv_link(publication))


class ArxivVersionsStage(EnrichmentStage):
    name = "arxiv_versions"
    progress_label = "Publications: looking up arXiv copies of papers without full text"

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        self.with_links = {row.publication_id for row in self.prepared.read_models("repo_links", RepoLink)
                           if row.links}
        found, failed = self._seed_native_pdfs(publications), 0
        candidates = [
            publication for publication in publications
            if self.selected("publications", publication.id)
            and self.needs_attempt(publication.processing.get(self.name))
            and wants_arxiv_copy(publication)
        ]
        client = SemanticScholarClient(self.config.request_timeout, self.config.semantic_scholar_api_key,
                                       requests_per_second=self.config.semantic_scholar_rps)
        batches = [candidates[start:start + MAX_BATCH] for start in range(0, len(candidates), MAX_BATCH)]
        for batch in self.progress(batches, total=len(batches), unit="batch"):
            dois = [bare_doi(publication.doi or "") for publication in batch]
            try:
                records = client.papers_by_doi(dois)
            except (HttpRequestError, ValueError) as exc:
                logger.warning("arxiv_versions: Semantic Scholar batch failed: %s", redact_text(exc))
                for publication in batch:
                    self._record(publication, ProcessingStatus.FAILED, 0, redact_text(exc))
                failed += len(batch)
            else:
                self.raw.append("semantic_scholar", {"records": records}, {"dois": dois})
                found += sum(self._apply(publication, arxiv_id(record))
                             for publication, record in zip(batch, records, strict=True))
            self.prepared.upsert_models("publications", batch)
        return {"arxiv_looked_up": len(candidates), "arxiv_found": found, "arxiv_failed": failed}

    def _seed_native_pdfs(self, publications: Iterable[Publication]) -> int:
        """Restore the primary candidate for native arXiv DOI records without a PDF URL."""
        changed = []
        for publication in publications:
            if not self.selected("publications", publication.id) or (publication.full_text or "").strip():
                continue
            updated = False
            if url := native_arxiv_pdf_url(publication):
                publication.pdf_urls.append(url)
                updated = True
            for record in publication.source_records:
                if not record.pdf_urls and (identity := arxiv_doi_identity(record.doi or "")):
                    record_url = f"https://arxiv.org/pdf/{''.join(identity)}"
                    record.pdf_urls.append(record_url)
                    if record_url not in publication.pdf_urls:
                        publication.pdf_urls.append(record_url)
                    updated = True
            if updated:
                publication.processing.pop("code_links", None)
                changed.append(publication)
        self.prepared.upsert_models("publications", changed)
        return len(changed)

    def _apply(self, publication: Publication, identifier: str | None) -> int:
        identity = _parse_arxiv_id(identifier) if identifier else None
        if identity is None:
            self._record(publication, ProcessingStatus.COMPLETED_EMPTY, 0)
            return 0
        url = f"https://arxiv.org/pdf/{''.join(identity)}"
        publication.pdf_urls.append(url)
        publication.arxiv_sources.append(ArxivSource(url=url))
        if publication.id not in self.with_links:
            publication.processing.pop("code_links", None)
        self._record(publication, ProcessingStatus.COMPLETED, 1)
        return 1

    def _record(self, publication: Publication, status: ProcessingStatus, count: int,
                error: str | None = None) -> None:
        state = publication.processing.get(self.name)
        publication.processing[self.name] = ProcessingState(
            status=status,
            attempts=(state.attempts if state else 0) + 1,
            finished_at=datetime.now(UTC),
            result_count=count,
            error=error,
        )
