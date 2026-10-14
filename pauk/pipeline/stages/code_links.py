from __future__ import annotations

import logging
import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlencode, urlparse

import pymupdf as fitz

from pauk.models import ClassificationStatus, CodeLink, LinkOccurrence, Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.redaction import redact_text
from pauk.sources.base import HttpClient
from pauk.storage import PdfStore
from pauk.urls import normalize_repo_url

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

_WRAP = r"(?:-\n[ \t]*|(?<=_)\n[ \t]*)?"
_CHAR = r"(?:-(?!\n)|[\w.])"
_SEGMENT = _CHAR + r"+(?:" + _WRAP + _CHAR + r"+)*"
GITHUB_URL = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.)?github"
    + _WRAP
    + r"\.com"
    + _WRAP
    + r"/"
    + _SEGMENT
    + r"/"
    + _SEGMENT,
    re.IGNORECASE,
)
_EMBEDDED_WRAP = re.compile(r"-\n[ \t]*")
_AMBIGUOUS_WRAP = re.compile(r"-\n[ \t]*|(?<=_)\n[ \t]*")
URL_TRAILING_PUNCT = ".,;:!?)]}>\"'-"
GITHUB_HOST = "github.com"
_GLUED_TAIL = re.compile(r"\.(?:[A-Z][a-z]+[\w-]*|[A-Z]{2,}[\w-]*|\d+(?:\.\d+)*)$")
_REFERENCE_HEADING = re.compile(
    r"(?im)^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]+)?"
    r"(?:references|bibliography|литература|список (?:использованной )?литературы)"
    r"[ \t]*:?[ \t]*$"
)
_POST_REFERENCE_HEADING = re.compile(
    r"(?im)^[ \t]*(?:(?:S\d+|[A-Z]|\d+(?:\.\d+)*)[.)]?[ \t]+)?"
    r"(?:append(?:ix|ices)(?:[ \t]+[A-Z])?|"
    r"supplement(?:ary)?(?:[ \t]+(?:material(?:s)?|information|data))?|"
    r"supporting[ \t]+information|приложени[ея](?:[ \t]+[A-ZА-Я])?|"
    r"дополнительные[ \t]+материалы)"
    r"(?:[ \t]*[:.\u2013\u2014-][ \t]*[^\n]+)?[ \t]*$"
    r"|^[ \t]*(?-i:[A-Z](?:\.\d+)*(?:[ \t]+|[ \t]*\n[ \t]*)[A-Z][A-Za-z -]{2,80})[ \t]*$"
)
_REFERENCE_ENTRY = re.compile(
    r"(?m)^[ \t]*(?:\[(?P<bracket>\d{1,4})\][.):]?|(?P<plain>\d{1,4})[.)])"
    r"(?:[ \t]+|[ \t]*\n[ \t]*)"
)
_CITATION = re.compile(r"\[(?P<labels>\d[\d,; \t\u2013\u2014-]{0,80})\]")
_SUPERSCRIPT_NUMBER = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3}")
_FOOTNOTE_NUMBER = re.compile(r"^[ \t]*(\d{1,3})(?!\d)[.)]?")
_BODY_HEADING = re.compile(r"(?im)^[ \t]*(?:abstract|аннотация)(?=[\s:.\u2013\u2014-])")

CONTEXT_WINDOW = 500
CRAWLER_DOWNLOAD_TIMEOUT = 180


def _normalize_ligatures(text: str) -> str:
    """PDF fonts commonly render "ff"/"fi"/"fl"/... as a single ligature
    glyph (found on real papers: "caﬀe" vs "caffe" for the same repo, split
    into two different URLs). NFKC decomposes it back to plain letters -
    character-for-character, so unlike the line-wrap handling above this
    can't glue unrelated text together.
    """
    return unicodedata.normalize("NFKC", text)


DEPOSIT_TYPES = {"software", "dataset"}
REPOSITORY_ARCHIVE = re.compile(r"^([\w.-]+)/([\w.-]+):\s")
ARCHIVED_DEPOSIT_REASON = "repository_archived_by_this_deposit"


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
    punctuation and a glued-on sentence/footnote tail, add a scheme if the
    match was bare, then canonicalize."""
    url = _EMBEDDED_WRAP.sub("", raw.strip()).rstrip(URL_TRAILING_PUNCT)
    url = _GLUED_TAIL.sub("", url)
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return _canonical_github_url(url)


def _slice_context(text: str, start: int, end: int) -> str | None:
    window = text[max(0, start - CONTEXT_WINDOW) : end + CONTEXT_WINDOW]
    return " ".join(window.split()) or None


def _url_candidates(raw: str) -> list[str]:
    variants = [""]
    finished: list[str] = []
    start = 0
    for match in _AMBIGUOUS_WRAP.finditer(raw):
        variants = [prefix + raw[start:match.start()] for prefix in variants]
        if match.group().startswith("-"):
            variants = [prefix + suffix for prefix in variants for suffix in ("", "-")]
        else:
            finished.extend(variants)
        if len(variants) + len(finished) > 32:
            raise ValueError("Too many ambiguous line breaks in a GitHub URL")
        start = match.end()
    variants = finished + [prefix + raw[start:] for prefix in variants]
    return list(dict.fromkeys(
        url for variant in variants
        if urlparse(url := _clean_match(variant)).netloc.lower() == GITHUB_HOST
        and len(urlparse(url).path.strip("/").split("/")) == 2
    ))


def _occurrences_in_text(text: str, page_number: int | None) -> dict[str, list[LinkOccurrence]]:
    """Canonical URL -> every occurrence found in this text."""
    source = "abstract" if page_number is None else "pdf_text"
    found: dict[str, list[LinkOccurrence]] = defaultdict(list)
    for match in GITHUB_URL.finditer(text):
        raw = match.group()
        candidates = _url_candidates(raw)
        continuous = "\n" not in raw
        for url in candidates:
            found[url].append(LinkOccurrence(
                context=_slice_context(text, match.start(), match.end()), page_number=page_number,
                source=source, text_start=match.start(), text_end=match.end(),
                raw_url=raw, raw_fragments=[raw],
                candidate_urls=candidates, continuous=continuous,
            ))
    return dict(found)


def _annotation_context(
    page: fitz.Page, page_text: str, rect
) -> tuple[str | None, int | None, int | None]:
    """Context around a clickable label, e.g. a hyperlinked "here"."""
    if rect is None:
        return None, None, None
    try:
        raw_visible = cast(str, page.get_text("text", clip=rect)).strip()
        visible = _normalize_ligatures(raw_visible)
    except Exception:
        return None, None, None
    if not visible:
        return None, None, None
    # Match MuPDF's ASCII case folding so geometry and text offsets stay aligned.
    matches = list(re.finditer(re.escape(visible), page_text, re.IGNORECASE | re.ASCII))
    if not matches:
        return " ".join(visible.split()) or None, None, None
    match = matches[0]
    if len(matches) > 1:
        try:
            hits = page.search_for(raw_visible)
            if len(hits) != len(matches):
                return " ".join(visible.split()) or None, None, None
            target = fitz.Rect(rect)
            index = min(
                range(len(hits)),
                key=lambda current: (
                    hits[current].x0 + hits[current].x1 - target.x0 - target.x1
                ) ** 2 + (
                    hits[current].y0 + hits[current].y1 - target.y0 - target.y1
                ) ** 2,
            )
            match = matches[index]
        except Exception:
            return " ".join(visible.split()) or None, None, None
    return _slice_context(page_text, match.start(), match.end()), match.start(), match.end()


def _pdf_page_occurrences(
    page: fitz.Page, text: str, page_number: int
) -> dict[str, list[LinkOccurrence]]:
    """Everything found on one page: URLs spelled out in the text, plus GitHub
    links reachable only through a clickable annotation whose visible label
    doesn't spell out the URL (e.g. a hyperlinked "here")."""
    found = _occurrences_in_text(text, page_number)
    word_offsets = None
    for link in page.get_links() or []:
        uri = link.get("uri")
        if not uri or link.get("kind") != fitz.LINK_URI:
            continue
        url = _clean_match(uri)
        parsed = urlparse(url)
        if (
            parsed.netloc.lower() != GITHUB_HOST
            or len(parsed.path.strip("/").split("/")) != 2
        ):
            continue
        context, start, end = _annotation_context(page, text, link.get("from"))
        if start is None and link.get("from") is not None:
            if word_offsets is None:
                word_offsets = []
                cursor = 0
                try:
                    words = page.get_text("words")
                except Exception:
                    words = []
                for word in words:
                    visible = _normalize_ligatures(word[4])
                    offset = text.find(visible, cursor)
                    if offset >= 0:
                        cursor = offset + len(visible)
                        word_offsets.append((fitz.Rect(word[:4]), offset, cursor))
            target = fitz.Rect(link["from"])
            overlapping = [
                (offset, stop) for bounds, offset, stop in word_offsets
                if bounds.get_area() and (bounds & target).get_area() >= bounds.get_area() / 2
            ]
            if overlapping:
                start = min(offset for offset, _ in overlapping)
                end = max(stop for _, stop in overlapping)
                context = _slice_context(text, start, end)
        found.setdefault(url, []).append(LinkOccurrence(
            context=context, page_number=page_number, source="pdf_annotation",
            text_start=start, text_end=end, raw_url=uri, raw_fragments=[uri],
            candidate_urls=[url],
        ))
    return found


def _citation_labels(raw: str) -> set[str]:
    """Reference numbers named by one bracketed citation."""
    labels: set[str] = set()
    normalized = raw.replace("\u2013", "-").replace("\u2014", "-")
    for part in re.split(r"[,;]", normalized):
        part = part.strip()
        if not part:
            continue
        if match := re.fullmatch(r"(\d{1,4})\s*-\s*(\d{1,4})", part):
            start, end = map(int, match.groups())
            if start <= end <= start + 50:
                labels.update(str(number) for number in range(start, end + 1))
            continue
        if part.isdigit():
            labels.add(str(int(part)))
    return labels


def _pdf_footnotes(page: fitz.Page, text: str) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str]]]:
    """Find raised body markers and smaller numbered notes without changing text offsets."""
    markers = []
    definitions = []
    cursor = 0
    heading = _BODY_HEADING.search(text) if page.number == 0 else None
    body_start = heading.end() if heading else 0
    for block in page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)["blocks"]:
        lines = []
        for line in block.get("lines", []):
            spans = []
            for span in line["spans"]:
                normalized = _normalize_ligatures(span["text"])
                start = text.find(normalized, cursor)
                if start < 0 or not normalized:
                    continue
                cursor = start + len(normalized)
                spans.append((span, start, cursor))
                numbers = list(_SUPERSCRIPT_NUMBER.finditer(span["text"]))
                if span["flags"] & fitz.TEXT_FONT_SUPERSCRIPT and re.fullmatch(r"\d{1,3}", normalized.strip()):
                    numbers = list(re.finditer(r"\d{1,3}", span["text"]))
                for number in numbers:
                    marker_start = start + len(_normalize_ligatures(span["text"][:number.start()]))
                    marker_end = marker_start + len(_normalize_ligatures(number.group()))
                    if marker_start < body_start:
                        continue
                    # Exponents after a single variable or an italic math span
                    # must not become footnotes just because the label matches.
                    if not re.search(r"[^\W\d_]{2,}\d*[.!?)]?\s*$", text[max(0, marker_start - 80):marker_start]):
                        continue
                    previous = spans[-2][0] if len(spans) > 1 else span
                    if previous["flags"] & fitz.TEXT_FONT_ITALIC:
                        continue
                    markers.append((marker_start, marker_end, str(int(_normalize_ligatures(number.group()))),
                                    span["origin"][1], previous["size"]))
            if spans:
                lines.append(spans)
        for index, spans in enumerate(lines):
            first, start, _ = spans[0]
            line_end = spans[-1][2]
            number = _FOOTNOTE_NUMBER.match(text[start:line_end])
            if number is None or not text[start + number.end():line_end].strip():
                continue
            label = str(int(number.group(1)))
            # Several notes may share a block; a note never owns the next one.
            end = next((following[0][1] for following in lines[index + 1:]
                        if _FOOTNOTE_NUMBER.match(text[following[0][1]:following[-1][2]])),
                       lines[-1][-1][2])
            definitions.append((start, end, label, first["origin"][1], first["size"]))
    # The margin keeps body lists out despite small font-size rounding differences.
    notes = [
        (start, end, label) for start, end, label, y, size in definitions
        if any(marker_label == label and marker_y < y and size < body_size * 0.95
               for _, _, marker_label, marker_y, body_size in markers)
    ]
    return [(start, end, label) for start, end, label, _, _ in markers], notes


def _link_reference_contexts(
    pages: list[str], page_occurrences: list[dict[str, list[LinkOccurrence]]],
    footnotes: dict[int, tuple[list[tuple[int, int, str]], list[tuple[int, int, str]]]] | None = None,
) -> None:
    """Attach body citations to repository URLs found in numbered references."""
    if not any(page_occurrences):
        return
    reference_urls: dict[tuple[int | None, str], set[str]] = defaultdict(set)
    reference_candidates: dict[tuple[int | None, str], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    excluded_ranges: list[list[tuple[int, int]]] = [[] for _ in pages]
    in_references = False
    references_finished = False
    sections = []

    def record_reference(label: str | None, url: str, occurrence: LinkOccurrence,
                         scope: int | None = None, superscript: bool = False) -> None:
        occurrence.source = "reference"
        occurrence.reference_label = label
        if label is not None:
            key = (scope, "^" + label if superscript else label)
            reference_urls[key].add(url)
            reference_candidates[key][url].update(occurrence.candidate_urls or [url])

    for page_index, text in enumerate(pages):
        if references_finished:
            continue
        section_start = 0
        reference_start = 0
        if not in_references:
            heading = _REFERENCE_HEADING.search(text)
            if heading is None:
                continue
            in_references = True
            section_start = heading.end()
            reference_start = heading.start()

        next_section = _POST_REFERENCE_HEADING.search(text, section_start)
        section_end = next_section.start() if next_section else len(text)
        excluded_ranges[page_index].append((reference_start, section_end))
        sections.append((page_index, section_start, section_end))
        if next_section:
            in_references = False
            references_finished = True

    # A PDF can put the number on its own line or split an entry across pages.
    reference_text = "\n".join(pages[index][start:end] for index, start, end in sections)
    cited_labels = {
        label for text in pages for citation in _CITATION.finditer(text)
        for label in _citation_labels(citation.group("labels"))
    }
    entries = list(_REFERENCE_ENTRY.finditer(reference_text))
    numbers = [int(entry.group("bracket") or entry.group("plain")) for entry in entries]
    entries = [
        entry for index, entry in enumerate(entries)
        if entry.group("bracket") or str(numbers[index]) in cited_labels
        or (numbers[index] < 1000 and (
            (index > 0 and numbers[index - 1] + 1 == numbers[index])
            or (index + 1 < len(numbers) and numbers[index] + 1 == numbers[index + 1])
        ))
    ]
    entry_starts = [entry.start() for entry in entries]
    section_offset = 0
    for page_index, start, end in sections:
        for url, occurrences in page_occurrences[page_index].items():
            for occurrence in occurrences:
                if occurrence.text_start is None or not start <= occurrence.text_start < end:
                    continue
                entry_index = bisect_right(entry_starts, section_offset + occurrence.text_start - start) - 1
                entry = entries[entry_index] if entry_index >= 0 else None
                label = str(int(entry.group("bracket") or entry.group("plain"))) if entry else None
                record_reference(label, url, occurrence)
        section_offset += end - start + 1

    # Numbered footnotes often have no heading. Link one only when the same
    # marker occurs earlier on that page, which keeps ordinary numbered lists
    # from being treated as references merely because they contain a URL.
    for page_index, text in enumerate(pages):
        _, notes = (footnotes or {}).get(page_index, ([], []))
        for start, end, label in notes:
            if any(left <= start < right for left, right in excluded_ranges[page_index]):
                continue
            linked = False
            for url, occurrences in page_occurrences[page_index].items():
                for occurrence in occurrences:
                    if occurrence.text_start is not None and start <= occurrence.text_start < end:
                        record_reference(label, url, occurrence, page_index, superscript=True)
                        linked = True
            if linked:
                excluded_ranges[page_index].append((start, end))
        entries = list(_REFERENCE_ENTRY.finditer(text))
        for index, entry in enumerate(entries):
            if any(start <= entry.start() < end
                   for start, end in excluded_ranges[page_index]):
                continue
            label = str(int(entry.group("bracket") or entry.group("plain")))
            cited_before = any(
                label in _citation_labels(citation.group("labels"))
                for citation in _CITATION.finditer(text, 0, entry.start())
            )
            if not cited_before:
                continue
            entry_end = entries[index + 1].start() if index + 1 < len(entries) else len(text)
            linked = False
            for url, occurrences in page_occurrences[page_index].items():
                for occurrence in occurrences:
                    if occurrence.text_start is None or not entry.start() <= occurrence.text_start < entry_end:
                        continue
                    record_reference(label, url, occurrence, page_index)
                    linked = True
            if linked:
                excluded_ranges[page_index].append((entry.start(), entry_end))

    if not reference_urls:
        return

    for page_index, text in enumerate(pages):
        citations = [
            (citation.start(), citation.end(), _citation_labels(citation.group("labels")), False)
            for citation in _CITATION.finditer(text)
        ]
        markers, _ = (footnotes or {}).get(page_index, ([], []))
        citations.extend((start, end, {label}, True) for start, end, label in markers)
        for citation_start, citation_end, labels, page_scoped in sorted(citations):
            if any(start <= citation_start < end
                   for start, end in excluded_ranges[page_index]):
                continue
            for label in sorted(labels):
                key = ((page_index, "^" + label) if page_scoped else
                       ((page_index, label) if (page_index, label) in reference_urls else (None, label)))
                urls = sorted(reference_urls.get(key, ()))
                for url in urls:
                    page_occurrences[page_index].setdefault(url, []).append(LinkOccurrence(
                        context=_slice_context(text, citation_start, citation_end),
                        page_number=page_index + 1,
                        source="citation",
                        reference_label=label,
                        text_start=citation_start,
                        text_end=citation_end,
                        candidate_urls=sorted(reference_candidates[key][url]),
                    ))


def _extract_pdf(data: bytes) -> tuple[list[str], list[dict[str, list[LinkOccurrence]]]]:
    """Per-page text (for full_text) and per-page link occurrences."""
    with fitz.open(stream=data, filetype="pdf") as doc:
        if not doc.is_pdf or doc.page_count == 0:
            raise ValueError("Response is not a non-empty PDF document")
        pages: list[str] = []
        page_occurrences: list[dict[str, list[LinkOccurrence]]] = []
        footnotes = {}
        for page in doc:
            text = _normalize_ligatures(cast(str, page.get_text()))
            pages.append(text)
            page_occurrences.append(_pdf_page_occurrences(page, text, len(pages)))
            if page_occurrences[-1]:
                footnotes[len(pages) - 1] = _pdf_footnotes(page, text)
        _link_reference_contexts(pages, page_occurrences, footnotes)
    return pages, page_occurrences


def _collect_occurrences(
    abstract: str,
    pdf_page_occurrences: list[dict[str, list[LinkOccurrence]]],
) -> dict[str, list[LinkOccurrence]]:
    """Canonical URL -> every place it was found, abstract first then PDF pages in order.

    page_number=None marks the abstract (it isn't paginated); PDF pages are
    1-indexed. dict insertion order keeps abstract-sourced URLs first, so
    Publication.code_url stays biased toward the abstract like before.
    """
    occurrences: dict[str, list[LinkOccurrence]] = defaultdict(list)
    for url, found in _occurrences_in_text(abstract, None).items():
        occurrences[url].extend(found)
    # Only uninterrupted visible text in this PDF can settle a wrapped spelling.
    confirmed = {
        (occurrence.pdf_id, urlparse(url).path.casefold()) for page_found in pdf_page_occurrences
        for url, found in page_found.items() for occurrence in found
        if occurrence.source in {"pdf_text", "reference"} and occurrence.continuous
    }
    for page_found in pdf_page_occurrences:
        for url, found in page_found.items():
            for occ in found:
                if len(occ.candidate_urls) > 1:
                    identity = urlparse(url).path.casefold()
                    groups = [_url_candidates(raw) for raw in occ.raw_fragments] or [occ.candidate_urls]
                    remaining = []
                    for group in groups:
                        supported = [candidate for candidate in group
                                     if (occ.pdf_id, urlparse(candidate).path.casefold()) in confirmed]
                        choices = supported or group
                        if identity in {urlparse(candidate).path.casefold() for candidate in choices}:
                            remaining.append(choices)
                    if not remaining:
                        continue
                    resolved = (occ.pdf_id, identity) in confirmed or any(len(group) == 1 for group in remaining)
                    choices = [url] if resolved else list(dict.fromkeys(
                        candidate for group in remaining for candidate in group
                    ))
                    occ = occ.model_copy(update={"candidate_urls": choices})
                occurrences[url].append(occ)
    return _merge_url_spellings(occurrences)


def _merge_url_spellings(occurrences: dict[str, list[LinkOccurrence]]) -> dict[str, list[LinkOccurrence]]:
    merged: dict[str, list[LinkOccurrence]] = {}
    spellings: dict[str, str] = {}
    for url, found in occurrences.items():
        spelling = spellings.setdefault(normalize_repo_url(url), url.strip())
        merged.setdefault(spelling, []).extend(found)
    return merged


def _preserve_pdf_occurrences(
    occurrences_by_url: dict[str, list[LinkOccurrence]],
    previous: RepoLink | None,
    failed_pdf_ids: set[str] | None = None,
) -> dict[str, list[LinkOccurrence]]:
    """Keep evidence from the last readable PDF when a retry cannot read it."""
    if previous is None:
        return occurrences_by_url
    for link in previous.links:
        preserved = [
            occ for occ in link.occurrences if occ.page_number is not None
            and (failed_pdf_ids is None or occ.pdf_id is None or occ.pdf_id in failed_pdf_ids)
        ]
        if not preserved:
            continue
        current = occurrences_by_url.setdefault(link.url, [])
        seen = {occ.model_dump_json() for occ in current}
        for occurrence in preserved:
            key = occurrence.model_dump_json()
            if key in seen:
                continue
            seen.add(key)
            current.append(occurrence)
    return occurrences_by_url


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
    progress_label = "Publications: finding code links in abstracts and PDFs"

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        links_by_publication = {
            row.publication_id: row for row in self.prepared.read_models("repo_links", RepoLink)
        }
        self.http = HttpClient(self.config.request_timeout)
        self.pdf_store = PdfStore(self.prepared.db, self.config.pdf_dir)
        self.crawler_available = self._crawler_available()
        candidates = [
            publication
            for publication in publications
            if self.selected("publications", publication.id)
            and self.needs_attempt(publication.processing.get(self.name))
        ]
        changed = 0
        for pub in self.progress(candidates, total=len(candidates)):
            state = pub.processing.get(self.name)
            archived = _archived_repository_url(pub)
            pdf_pages, pdf_page_occurrences, pdf_error = self._pdf_pages(pub)
            previous = links_by_publication.get(pub.id)
            missing_pdf_ids = {
                occ.pdf_id for link in previous.links for occ in link.occurrences
                if occ.pdf_id and occ.pdf_id not in self.parsed_pdf_ids
            } if previous else set()
            self.failed_pdf_ids.update(missing_pdf_ids)
            if missing_pdf_ids:
                pdf_error = "; ".join(filter(None, (pdf_error, f"PDF evidence unavailable: {', '.join(sorted(missing_pdf_ids))}")))
            if pdf_pages and (not pdf_error or not pub.full_text):
                # A transient failure on a later retry must not erase text a
                # previous successful run already extracted.
                pub.full_text = "\n\n".join(pdf_pages)
            occurrences_by_url = _collect_occurrences(
                _normalize_ligatures(pub.abstract or ""), pdf_page_occurrences
            )
            if pdf_error:
                occurrences_by_url = _preserve_pdf_occurrences(
                    occurrences_by_url, previous, self.failed_pdf_ids,
                )
            if archived and archived not in occurrences_by_url:
                # The deposit's own archived repo takes priority - it's what
                # code_url should point at, same as before this stage read PDFs.
                occurrences_by_url = {
                    archived: [LinkOccurrence(page_number=None, source="deposit_title")],
                    **occurrences_by_url,
                }
            occurrences_by_url = _merge_url_spellings(occurrences_by_url)
            archived_identity = normalize_repo_url(archived) if archived else None
            urls = list(occurrences_by_url)
            # Replacing the evidence invalidates any verdict made from the
            # previous contexts; the next link_relevance pass must judge the
            # new set even when that stage had already completed.
            pub.processing.pop("link_relevance", None)
            pub.processing[self.name] = ProcessingState(
                status=ProcessingStatus.FAILED
                if pdf_error
                else (ProcessingStatus.COMPLETED if urls else ProcessingStatus.COMPLETED_EMPTY),
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                result_count=len(urls),
                error=pdf_error,
            )
            links_by_publication[pub.id] = RepoLink(
                publication_id=pub.id,
                links=[
                    CodeLink(
                        url=url,
                        host=urlparse(url).netloc,
                        occurrences=occurrences,
                        **(
                            {
                                "classification_status": ClassificationStatus.CLASSIFIED,
                                "is_relevant": True,
                                "llm_confidence": 1.0,
                                "llm_reason": ARCHIVED_DEPOSIT_REASON,
                            }
                            if normalize_repo_url(url) == archived_identity
                            else {}
                        ),
                    )
                    for url, occurrences in occurrences_by_url.items()
                ],
            )
            changed += 1
        self.prepared.write_models("publications", publications)
        self.prepared.write_models("repo_links", links_by_publication.values())
        return {"publications": changed, "repo_links": len(links_by_publication)}

    def _crawler_available(self) -> bool:
        """Cheap, no-retry probe - PAUK_PDF_CRAWLER_URL unset means the fallback is off."""
        if not self.config.pdf_crawler_url:
            return False
        try:
            self.http.get_bytes(f"{self.config.pdf_crawler_url}/health", retries=0)
            return True
        except Exception as exc:
            logger.warning(
                "code_links: PDF-Crawler-Service unavailable at %s: %s",
                self.config.pdf_crawler_url,
                exc,
            )
            return False

    def _pdf_pages(
        self,
        pub: Publication,
    ) -> tuple[list[str], list[dict[str, list[LinkOccurrence]]], str | None]:
        version_ids = dict.fromkeys([*pub.merged_ids, *(version.openalex_id for version in pub.versions)])
        cached_versions = [pdf_id for pdf_id in version_ids if pdf_id != pub.id and self.pdf_store.exists(pdf_id)]
        # An already cached version makes a new download of a missing survivor unnecessary.
        if cached_versions and not self.pdf_store.exists(pub.id):
            pages, occurrences, error = [], [], None
        else:
            pages, occurrences, error = self._primary_pdf_pages(pub)
        self.parsed_pdf_ids = {pub.id} if pages else set()
        self.failed_pdf_ids = {pub.id} if error else set()
        for page_found in occurrences:
            for found in page_found.values():
                for occurrence in found:
                    occurrence.pdf_id = pub.id
        # Merged records can retain a more complete cached PDF than the survivor.
        errors = [error] if error else []
        for pdf_id in cached_versions:
            try:
                version_pages, version_occurrences = _extract_pdf(self.pdf_store.read(pdf_id))
            except Exception as exc:
                self.failed_pdf_ids.add(pdf_id)
                errors.append(f"{pdf_id}: {redact_text(exc)}")
                continue
            self.parsed_pdf_ids.add(pdf_id)
            for page_found in version_occurrences:
                for found in page_found.values():
                    for occurrence in found:
                        occurrence.pdf_id = pdf_id
            pages.extend(version_pages)
            occurrences.extend(version_occurrences)
        if cached_versions and not pages and not self.pdf_store.exists(pub.id):
            # Broken version caches must not prevent recovery from the primary sources.
            primary_pages, primary_occurrences, primary_error = self._primary_pdf_pages(pub)
            if primary_error:
                self.failed_pdf_ids.add(pub.id)
                errors.append(primary_error)
            if primary_pages:
                self.parsed_pdf_ids.add(pub.id)
                for page_found in primary_occurrences:
                    for found in page_found.values():
                        for occurrence in found:
                            occurrence.pdf_id = pub.id
                pages.extend(primary_pages)
                occurrences.extend(primary_occurrences)
        return pages, occurrences, "; ".join(errors) or None

    def _primary_pdf_pages(
        self,
        pub: Publication,
    ) -> tuple[list[str], list[dict[str, list[LinkOccurrence]]], str | None]:
        """Download (if not already cached) and extract per-page text + link occurrences.

        Tries every direct PDF candidate before falling back to the
        PDF-Crawler-Service (resolves a PDF from the DOI - arXiv, Unpaywall,
        publisher pages, ...) when it's configured and reachable.

        Returns (pages, page_occurrences, error). On any failure both lists
        are empty and error carries the reason (e.g. a 403 for a
        non-open-access PDF) — the caller still falls back to the
        abstract-only result rather than losing it.
        """
        errors = []
        if self.pdf_store.exists(pub.id):
            try:
                pages, occurrences = _extract_pdf(self.pdf_store.read(pub.id))
                return pages, occurrences, None
            except Exception as exc:
                errors.append(redact_text(exc))

        sources = [(url, {}) for url in pub.pdf_urls]
        if self.crawler_available and pub.doi:
            sources.append((
                f"{self.config.pdf_crawler_url}/download?" + urlencode({"url": pub.doi}),
                {"timeout": CRAWLER_DOWNLOAD_TIMEOUT, "retries": 0},
            ))
        for source_url, kwargs in sources:
            try:
                pdf_bytes = self.http.get_bytes(source_url, **kwargs)
                pages, occurrences = _extract_pdf(pdf_bytes)
                # Error pages must never poison the cache for subsequent attempts.
                self.pdf_store.save(pub.id, pdf_bytes)
                return pages, occurrences, None
            except Exception as exc:
                errors.append(redact_text(exc))
        return [], [], "; ".join(errors) or None
