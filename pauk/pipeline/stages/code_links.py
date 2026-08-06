from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlencode, urlparse

import fitz

from pauk.models import CodeLink, LinkOccurrence, Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.sources.base import HttpClient

from .base import EnrichmentStage

_WRAP = r"(?:-\n[ \t]*)?"
_CHAR = r"(?:-(?!\n)|[\w.])"
_SEGMENT = _CHAR + r"+(?:" + _WRAP + _CHAR + r"+)*"
GITHUB_URL = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.)?github" + _WRAP + r"\.com" + _WRAP + r"/"
    + _SEGMENT + r"/" + _SEGMENT, re.IGNORECASE)
_EMBEDDED_WRAP = re.compile(r"-\n[ \t]*")
URL_TRAILING_PUNCT = ".,;:!?)]}>\"'-"
GITHUB_HOST = "github.com"

CONTEXT_WINDOW = 500

# Work types that are a deposit of something rather than a paper about it.
DEPOSIT_TYPES = {"software", "dataset"}
# How GitHub titles the snapshot it archives on Zenodo for a release:
# "asl/BandageNG: Continuous build". The owner/name part carries no spaces,
# which keeps titles like "A/B testing: results" out.
REPOSITORY_ARCHIVE = re.compile(r"^([\w.-]+)/([\w.-]+):\s")


def _canonical_github_url(url: str) -> str:
    """Collapse to https://github.com/owner/repo - the repo's identity,

    regardless of a deeper path (/tree/main, /blob/..., a trailing slash) or
    a "www." host. Real PDF hyperlinks (unlike our own regex matches) can
    point deep into a repo, so this can't just trust a 2-segment path.
    """
    parsed = urlparse(url)
    netloc = "github.com" if parsed.netloc.lower() == "www.github.com" else parsed.netloc
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return parsed._replace(netloc=netloc).geturl()
    owner, repo = segments[0], segments[1].removesuffix(".git")
    return f"{parsed.scheme}://{netloc}/{owner}/{repo}"


def _clean_match(raw: str) -> str:
    """Drop any embedded line-wrap the match itself spans, strip trailing
    punctuation, add a scheme if the match was bare, then canonicalize."""
    url = _EMBEDDED_WRAP.sub("", raw).rstrip(URL_TRAILING_PUNCT)
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return _canonical_github_url(url)


def _slice_context(text: str, start: int, end: int) -> str | None:
    window = text[max(0, start - CONTEXT_WINDOW):end + CONTEXT_WINDOW]
    return " ".join(window.split()) or None


def _occurrences_in_text(text: str, page_number: int | None) -> dict[str, LinkOccurrence]:
    """Canonical URL -> first occurrence found in this text.

    One entry per URL: a link repeated within the same page/abstract adds no
    new information, it just restates the same context.
    """
    found: dict[str, LinkOccurrence] = {}
    for match in GITHUB_URL.finditer(text):
        url = _clean_match(match.group())
        if url in found:
            continue
        found[url] = LinkOccurrence(
            context=_slice_context(text, match.start(), match.end()), page_number=page_number)
    return found


def _annotation_context(page: fitz.Page, page_text: str, rect) -> str | None:
    """The clickable rectangle's own visible label, e.g. a hyperlinked "here" -
    that's the only context a bare-URI annotation can offer."""
    if rect is None:
        return None
    try:
        visible = cast(str, page.get_text("text", clip=rect)).strip()
    except Exception:
        return None
    if not visible:
        return None
    idx = page_text.find(visible)
    if idx < 0:
        return " ".join(visible.split()) or None
    return _slice_context(page_text, idx, idx + len(visible))


def _pdf_page_occurrences(page: fitz.Page, text: str, page_number: int) -> dict[str, LinkOccurrence]:
    """Everything found on one page: URLs spelled out in the text, plus GitHub
    links reachable only through a clickable annotation whose visible label
    doesn't spell out the URL (e.g. a hyperlinked "here")."""
    found = _occurrences_in_text(text, page_number)
    for link in page.get_links() or []:
        uri = link.get("uri")
        if not uri or link.get("kind") != fitz.LINK_URI:
            continue
        url = _clean_match(uri)
        if urlparse(url).netloc.lower() != GITHUB_HOST or url in found:
            continue
        found[url] = LinkOccurrence(
            context=_annotation_context(page, text, link.get("from")), page_number=page_number)
    return found


def _extract_pdf(path: Path) -> tuple[list[str], list[dict[str, LinkOccurrence]]]:
    """Per-page text (for full_text) and per-page link occurrences."""
    with fitz.open(str(path)) as doc:
        pages: list[str] = []
        page_occurrences: list[dict[str, LinkOccurrence]] = []
        for page in doc:
            text = cast(str, page.get_text())
            pages.append(text)
            page_occurrences.append(_pdf_page_occurrences(page, text, len(pages)))
    return pages, page_occurrences


def _collect_occurrences(
    abstract: str, pdf_page_occurrences: list[dict[str, LinkOccurrence]],
) -> dict[str, list[LinkOccurrence]]:
    """Canonical URL -> every place it was found, abstract first then PDF pages in order.

    page_number=None marks the abstract (it isn't paginated); PDF pages are
    1-indexed. dict insertion order keeps abstract-sourced URLs first, so
    Publication.code_url stays biased toward the abstract like before.
    """
    occurrences: dict[str, list[LinkOccurrence]] = defaultdict(list)
    for url, occ in _occurrences_in_text(abstract, None).items():
        occurrences[url].append(occ)
    for page_found in pdf_page_occurrences:
        for url, occ in page_found.items():
            occurrences[url].append(occ)
    return dict(occurrences)


def _archived_repository_url(publication: Publication) -> str | None:
    """The repository a software or dataset deposit is an archive of.

    Zenodo mints a DOI for every GitHub release, and OpenAlex indexes each
    one as a work of its own — which is why "asl/BandageNG: Continuous
    build" sits in the graph looking like a paper. The repository it
    archives is named in the title, so the deposit can point at it instead
    of standing alone.
    """
    if publication.type not in DEPOSIT_TYPES:
        return None
    match = REPOSITORY_ARCHIVE.match(publication.title or "")
    if not match:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


class CodeLinksStage(EnrichmentStage):
    name = "code_links"

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        links_by_publication = {
            row.publication_id: row
            for row in self.prepared.read_models("repo_links", RepoLink)
        }
        group = self.prepared.group_dir.name
        http = HttpClient(self.config.request_timeout)
        # One probe per run, not per publication - an unreachable crawler
        # shouldn't add a failed request to every single row.
        crawler_available = self._crawler_available(http)
        changed = 0
        for pub in publications:
            if self.selection is not None and (
                self.selection.entity != "publications" or pub.id not in self.selection.ids
            ):
                continue
            state = pub.processing.get(self.name)
            if not self.needs_attempt(state):
                continue
            archived = _archived_repository_url(pub)
            needs_pdf = bool(pub.pdf_url) or (crawler_available and bool(pub.doi))
            if needs_pdf:
                pdf_pages, pdf_page_occurrences, pdf_error = self._pdf_pages(http, pub, group, crawler_available)
            else:
                pdf_pages, pdf_page_occurrences, pdf_error = [], [], None
            if pdf_pages:
                # A transient failure on a later retry must not erase text a
                # previous successful run already extracted.
                pub.full_text = "\n\n".join(pdf_pages)
            occurrences_by_url = _collect_occurrences(pub.abstract or "", pdf_page_occurrences)
            if archived and archived not in occurrences_by_url:
                # The deposit's own archived repo takes priority - it's what
                # code_url should point at, same as before this stage read PDFs.
                occurrences_by_url = {archived: [LinkOccurrence(page_number=None)], **occurrences_by_url}
            urls = list(occurrences_by_url)
            pub.has_code = bool(urls)
            pub.code_url = urls[0] if urls else None
            pub.processing[self.name] = ProcessingState(
                status=ProcessingStatus.FAILED if pdf_error else (
                    ProcessingStatus.COMPLETED if urls else ProcessingStatus.COMPLETED_EMPTY),
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC), result_count=len(urls), error=pdf_error,
            )
            links_by_publication[pub.id] = RepoLink(publication_id=pub.id, links=[
                CodeLink(url=url, host=urlparse(url).netloc, occurrences=occurrences,
                         is_relevant=True, llm_confidence=1.0,
                         llm_reason=("repository_archived_by_this_deposit" if url == archived
                                     else "github_url_in_text"))
                for url, occurrences in occurrences_by_url.items()
            ])
            changed += 1
        self.prepared.write_models("publications", publications)
        self.prepared.write_models("repo_links", links_by_publication.values())
        return {"publications": changed, "repo_links": len(links_by_publication)}

    def _crawler_available(self, http: HttpClient) -> bool:
        """Cheap, no-retry probe - PAUK_PDF_CRAWLER_URL unset means the fallback is off."""
        if not self.config.pdf_crawler_url:
            return False
        try:
            http.get_bytes(f"{self.config.pdf_crawler_url}/health", retries=0)
            return True
        except Exception:
            return False

    def _pdf_pages(
        self, http: HttpClient, pub: Publication, group: str, crawler_available: bool,
    ) -> tuple[list[str], list[dict[str, LinkOccurrence]], str | None]:
        """Download (if not already cached) and extract per-page text + link occurrences.

        Prefers pub.pdf_url; if OpenAlex supplied none, falls back to the
        PDF-Crawler-Service (resolves a PDF from the DOI - arXiv, Unpaywall,
        publisher pages, ...) when it's configured and reachable.

        Returns (pages, page_occurrences, error). On any failure both lists
        are empty and error carries the reason (e.g. a 403 for a
        non-open-access PDF) — the caller still falls back to the
        abstract-only result rather than losing it.
        """
        if pub.pdf_url:
            source_url = pub.pdf_url
        elif crawler_available and pub.doi:
            source_url = f"{self.config.pdf_crawler_url}/download?" + urlencode(
                {"url": f"https://doi.org/{pub.doi}"})
        else:
            return [], [], None
        path = self.config.pdf_dir / group / f"{pub.id}.pdf"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".pdf.tmp")
                tmp.write_bytes(http.get_bytes(source_url))
                tmp.replace(path)
            pages, page_occurrences = _extract_pdf(path)
            return pages, page_occurrences, None
        except Exception as exc:
            return [], [], str(exc)
