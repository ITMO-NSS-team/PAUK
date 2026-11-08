"""Append an arXiv PDF link to papers without full text, found by DOI via Semantic Scholar.

Runs before code_links, which reads the link when the publisher's copy fails.
A paper with no code links yet has its code_links state dropped so the same
run reads it; one that already has links keeps them and waits for --force.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pauk.models import Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.redaction import redact_text
from pauk.sources.base import HttpRequestError
from pauk.sources.semantic_scholar import MAX_BATCH, SemanticScholarClient, arxiv_id

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")


def bare_doi(doi: str) -> str:
    value = doi.strip()
    for prefix in DOI_PREFIXES:
        if value.lower().startswith(prefix):
            return value[len(prefix):]
    return value


def has_arxiv_link(publication: Publication) -> bool:
    return any((urlsplit(url).hostname or "").lower() in ARXIV_HOSTS for url in publication.pdf_urls)


def wants_arxiv_copy(publication: Publication) -> bool:
    return (bool(publication.doi) and not (publication.full_text or "").strip()
            and not has_arxiv_link(publication))


class ArxivVersionsStage(EnrichmentStage):
    name = "arxiv_versions"
    progress_label = "Publications: looking up arXiv copies of papers without full text"

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        candidates = [
            publication for publication in publications
            if self.selected("publications", publication.id)
            and self.needs_attempt(publication.processing.get(self.name))
            and wants_arxiv_copy(publication)
        ]
        self.with_links = {row.publication_id for row in self.prepared.read_models("repo_links", RepoLink)
                           if row.links}
        client = SemanticScholarClient(self.config.request_timeout, self.config.semantic_scholar_api_key,
                                       requests_per_second=self.config.semantic_scholar_rps)
        batches = [candidates[start:start + MAX_BATCH] for start in range(0, len(candidates), MAX_BATCH)]
        found = failed = 0
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

    def _apply(self, publication: Publication, identifier: str | None) -> int:
        if identifier is None:
            self._record(publication, ProcessingStatus.COMPLETED_EMPTY, 0)
            return 0
        publication.pdf_urls = [*publication.pdf_urls, f"https://arxiv.org/pdf/{identifier}"]
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
