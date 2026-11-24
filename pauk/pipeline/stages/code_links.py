from __future__ import annotations

import logging
import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
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

from .arxiv_versions import arxiv_doi_identity, arxiv_pdf_identity, arxiv_preprints, native_arxiv_pdf_url
from .base import EnrichmentStage

logger = logging.getLogger(__name__)

_LINE_WRAP = r"(?:-\n[ \t]*|(?<=_)\n[ \t]*)"
_WRAP = _LINE_WRAP + "?"
_CHAR = r"(?:-(?!\n)|[\w.])"
_SEGMENT = _CHAR + r"+(?:" + _LINE_WRAP + _CHAR + r"+)*"
GITHUB_URL = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.)?github"
    + _WRAP
    + r"\.com"
    + _WRAP
    + r"/"
    + "(?>"
    + _SEGMENT
    + r")/"
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
    """Decompose typographic ligatures ("ﬀ", "ﬁ") back to plain letters.

    PDF fonts render "ff"/"fi"/"fl" as one glyph, which splits one repo into
    two URLs ("caﬀe" vs "caffe"). NFKC is character-for-character, so unlike
    line-wrap handling it cannot glue unrelated text together.
    """
    return unicodedata.normalize("NFKC", text)


DEPOSIT_TYPES = {"software", "dataset"}
REPOSITORY_ARCHIVE = re.compile(r"^([\w.-]+)/([\w.-]+):\s")
ARCHIVED_DEPOSIT_REASON = "repository_archived_by_this_deposit"


def _canonical_github_url(url: str) -> str:
    """Collapse to https://github.com/owner/repo, the repo's identity.

    Ignores a deeper path (/tree/main, /blob/..., a trailing slash) and a
    "www." host. PDF hyperlinks can point deep into a repo, so a 2-segment
    path cannot be assumed.
    """
    parsed = urlparse(url)
    netloc = "github.com" if parsed.netloc.lower() == "www.github.com" else parsed.netloc
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return parsed._replace(netloc=netloc).geturl()
    owner, repo = segments[0], segments[1].removesuffix(".git")
    return f"{parsed.scheme}://{netloc}/{owner}/{repo}"


def _clean_match(raw: str) -> str:
    """Clean a raw regex match into a canonical URL.

    Drops embedded line-wraps, trailing punctuation and a glued-on
    sentence/footnote tail, and adds a scheme if the match was bare.
    """
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
    """Everything found on one page.

    URLs spelled out in the text, plus GitHub links reachable only through an
    annotation whose visible label does not spell out the URL.
    """
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
    1-indexed. Insertion order keeps abstract-sourced URLs first, so
    Publication.code_url favours the abstract.
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


def _tag_pdf_occurrences(
    page_occurrences: list[dict[str, list[LinkOccurrence]]],
    pdf_id: str,
) -> None:
    """Attach one source-record cache identity to extracted occurrences."""
    for page_found in page_occurrences:
        for found in page_found.values():
            for occurrence in found:
                occurrence.pdf_id = pdf_id


def _archived_repository_url(publication: Publication) -> str | None:
    """The repository a software or dataset deposit is an archive of.

    Zenodo mints a DOI for every GitHub release and OpenAlex indexes each as a
    work of its own (e.g. "asl/BandageNG: Continuous build"). The archived
    repository is named in the title, so the deposit can point at it.
    """
    if publication.type not in DEPOSIT_TYPES:
        return None
    match = REPOSITORY_ARCHIVE.match(publication.title or "")
    if not match:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


def _invalidate_automatic_verdict(link: CodeLink) -> bool:
    """Clear the verdict for changed automatic evidence, but keep deterministic archives."""
    if link.llm_reason == ARCHIVED_DEPOSIT_REASON:
        return False
    link.classification_status = ClassificationStatus.PENDING
    link.is_relevant = None
    link.llm_confidence = None
    link.llm_reason = None
    return True


def _is_manual_link(link: CodeLink) -> bool:
    """Recognize the evidence-free positive verdict written by the manual-add recipe."""
    return (not link.occurrences and link.is_relevant is True
            and link.llm_confidence is None and link.llm_reason is None)


def _remove_stale_pdf_evidence(
    row: RepoLink, fresh: dict[str, list[LinkOccurrence]], reread: set[str],
) -> bool:
    """Remove only automatic evidence absent from a successfully reread PDF."""
    extracted_sources = {"pdf_text", "pdf_annotation", "reference", "citation"}
    fresh_keys = {normalize_repo_url(url): {o.model_dump_json() for o in occurrences}
                  for url, occurrences in fresh.items()}
    retained = []
    reopen = False
    for link in row.links:
        known = fresh_keys.get(normalize_repo_url(link.url), set())
        occurrences = [o for o in link.occurrences if (
            o.pdf_id not in reread or o.source not in extracted_sources or o.model_dump_json() in known
        )]
        if len(occurrences) != len(link.occurrences):
            link.occurrences = occurrences
            if not occurrences and not known and link.llm_reason != ARCHIVED_DEPOSIT_REASON:
                reopen = True
                continue
            reopen |= _invalidate_automatic_verdict(link)
        retained.append(link)
    row.links = retained
    return reopen


def _merge_additional_evidence(
    row: RepoLink, evidence: dict[str, list[LinkOccurrence]], archived: str | None,
) -> bool:
    """Append distinct occurrences and reopen only verdicts touched by new evidence."""
    by_url: dict[str, CodeLink] = {}
    for link in row.links:
        key = normalize_repo_url(link.url)
        if key not in by_url or (_is_manual_link(by_url[key]) and not _is_manual_link(link)):
            by_url[key] = link
    reopen = False
    for url, occurrences in evidence.items():
        identity = normalize_repo_url(url)
        link = by_url.get(identity)
        if link is not None and _is_manual_link(link):
            continue
        if link is None:
            link = CodeLink(url=url, host=urlparse(url).netloc)
            if archived and identity == normalize_repo_url(archived):
                link.classification_status = ClassificationStatus.CLASSIFIED
                link.is_relevant = True
                link.llm_confidence = 1.0
                link.llm_reason = ARCHIVED_DEPOSIT_REASON
            row.links.append(link)
            by_url[identity] = link
            reopen = True
        seen = {occ.model_dump_json() for occ in link.occurrences}
        added = False
        for occurrence in occurrences:
            key = occurrence.model_dump_json()
            if key not in seen:
                seen.add(key)
                link.occurrences.append(occurrence)
                added = True
        if added:
            _invalidate_automatic_verdict(link)
        reopen |= link.classification_status == ClassificationStatus.PENDING
    return reopen


PdfEvidence = list[dict[str, list[LinkOccurrence]]]
PdfPages = tuple[list[str], PdfEvidence, str | None]
ArxivPdfPages = tuple[list[str], PdfEvidence, str | None, str | None]


def _canonical_arxiv_pdf(url: str) -> str | None:
    """Record the public arXiv identity without URL query parameters or fragments."""
    identity = arxiv_pdf_identity(url, pdf_only=True)
    return f"https://arxiv.org/pdf/{''.join(identity)}" if identity else None


def _compatible_arxiv_pdf(requested: str, actual: str | None) -> bool:
    """An explicit revision requires an exact match; an unversioned URL does not claim one."""
    expected = arxiv_pdf_identity(requested)
    observed = arxiv_pdf_identity(actual, pdf_only=True) if actual else None
    return bool(expected and observed and expected[0] == observed[0]
                and (not expected[1] or expected[1] == observed[1]))


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
            and (self.needs_attempt(publication.processing.get(self.name))
                 or any(self.needs_attempt(source.state) for source in publication.arxiv_sources))
        ]
        self.preprints = arxiv_preprints(publications) if any(pub.arxiv_sources for pub in candidates) else {}
        changed = 0
        for pub in self.progress(candidates, total=len(candidates)):
            if pub.arxiv_sources:
                previous = links_by_publication.get(pub.id)
                links_by_publication[pub.id] = self._process_with_arxiv(pub, previous)
                changed += 1
                continue
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
                # The deposit's own archived repo takes priority as code_url.
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
        self.prepared.write_models("repo_links", links_by_publication.values())
        self.prepared.write_models("publications", publications)
        return {"publications": changed, "repo_links": len(links_by_publication)}

    def _process_with_arxiv(self, pub: Publication, previous: RepoLink | None) -> RepoLink:
        """Collect the primary and supplemental sources without discarding earlier evidence."""
        row = previous if previous is not None else RepoLink(publication_id=pub.id)
        archived = _archived_repository_url(pub)
        main_state = pub.processing.get(self.name)
        run_main = self.needs_attempt(main_state)
        reread: set[str] = set()
        pages, page_evidence, main_error = (
            self._pdf_pages(pub, include_crawler=False, reread=reread) if run_main else ([], [], None)
        )
        supplemental_text, supplemental_evidence = self._process_arxiv_sources(pub, reread)
        arxiv_has_text = any(
            "\n\n".join(source_pages).strip() for source_pages in supplemental_text
        ) or bool((pub.full_text or "").strip())
        arxiv_completed = any(source.state.status == ProcessingStatus.COMPLETED for source in pub.arxiv_sources)
        arxiv_completed_empty = any(
            source.state.status == ProcessingStatus.COMPLETED_EMPTY for source in pub.arxiv_sources
        )
        arxiv_readable = arxiv_completed or bool(supplemental_evidence) or (arxiv_completed_empty and arxiv_has_text)
        if run_main and not pages and not arxiv_readable:
            crawler_targets = [
                (record.key, record.doi) for record in pub.source_records if record.doi
            ] or ([(pub.id, pub.doi)] if pub.doi else [])
            for record_key, doi in crawler_targets:
                fallback_pages, fallback_evidence, fallback_error = self._download_primary_pdf(
                    record_key, self._crawler_sources(doi),
                )
                if fallback_pages:
                    pages, page_evidence, main_error = fallback_pages, fallback_evidence, None
                    reread.add(record_key)
                    _tag_pdf_occurrences(page_evidence, record_key)
                    break
                if fallback_error:
                    main_error = "; ".join(filter(None, (main_error, fallback_error)))
        self._update_full_text(pub, pages, main_error, supplemental_text)
        evidence = _collect_occurrences(_normalize_ligatures(pub.abstract or ""), page_evidence) if run_main else {}
        if run_main and archived:
            evidence = {archived: [LinkOccurrence(source="deposit_title"), *evidence.pop(archived, [])], **evidence}
        for url, occurrences in supplemental_evidence.items():
            evidence.setdefault(url, []).extend(occurrences)
        evidence = _merge_url_spellings(evidence)
        reopen = self.force and bool(reread) and _remove_stale_pdf_evidence(row, evidence, reread)
        reopen |= _merge_additional_evidence(row, evidence, archived)
        if reopen:
            pub.processing.pop("link_relevance", None)
        if run_main:
            pub.processing[self.name] = self._extraction_state(main_state, len(row.links),
                                                               None if arxiv_readable and not pages else main_error)
        return row

    @staticmethod
    def _update_full_text(
        pub: Publication,
        primary_pages: list[str],
        primary_error: str | None,
        supplemental_pages: Iterable[list[str]],
    ) -> None:
        """Update full text from a clean primary read, or fill an empty gap from preprints."""
        primary_text = "\n\n".join(primary_pages)
        if primary_text.strip() and (not primary_error or not (pub.full_text or "").strip()):
            pub.full_text = primary_text
            return
        if not (pub.full_text or "").strip():
            for pages in supplemental_pages:
                if (text := "\n\n".join(pages)).strip():
                    pub.full_text = text
                    break

    def _process_arxiv_sources(
        self, pub: Publication, reread: set[str],
    ) -> tuple[list[list[str]], dict[str, list[LinkOccurrence]]]:
        """Parse each due arXiv source and persist its status independently of the main PDF."""
        texts = []
        combined: dict[str, list[LinkOccurrence]] = defaultdict(list)
        for source in pub.arxiv_sources:
            if not self.needs_attempt(source.state):
                continue
            pages, page_evidence, pdf_id, error = self._arxiv_pdf_pages(pub.id, source.url)
            evidence = _collect_occurrences("", page_evidence)
            source.state = self._extraction_state(source.state, len(evidence), error)
            if pdf_id is not None:
                reread.add(pdf_id)
                texts.append(pages)
                for url, occurrences in evidence.items():
                    combined[url].extend(occurrences)
        return texts, combined

    @staticmethod
    def _extraction_state(previous: ProcessingState | None, count: int, error: str | None) -> ProcessingState:
        """Record an extraction attempt without coupling independent sources."""
        return ProcessingState(
            status=ProcessingStatus.FAILED if error else (
                ProcessingStatus.COMPLETED if count else ProcessingStatus.COMPLETED_EMPTY
            ),
            attempts=(previous.attempts if previous else 0) + 1,
            finished_at=datetime.now(UTC),
            result_count=count,
            error=error,
        )

    def _arxiv_pdf_pages(self, publication_id: str, url: str) -> ArxivPdfPages:
        """Use a verified preprint cache or keep the targeted PDF under its own identity."""
        identity = arxiv_pdf_identity(url, pdf_only=True)
        if identity is None:
            return [], [], None, "Invalid arXiv PDF source"
        base, revision = identity
        matches = [preprint for preprint in self.preprints.get(base, [])
                   if preprint.publication_id != publication_id and (not revision or preprint.revision == revision)]
        if len({match.publication_id for match in matches}) != 1:
            matches = []
        compatible = [match for match in matches if match.cache_compatible]
        for candidate in compatible:
            candidate_url = url + candidate.revision if not revision and candidate.revision else url
            if not self.pdf_store.exists(candidate.cache_key):
                continue
            try:
                data, source = self.pdf_store.read_with_source(candidate.cache_key)
                if _compatible_arxiv_pdf(candidate_url, source):
                    pages, evidence = _extract_pdf(data)
                    _tag_pdf_occurrences(evidence, candidate.cache_key)
                    return pages, evidence, candidate.cache_key, None
            except Exception as exc:
                logger.debug("code_links: could not parse arXiv cache %s: %s", candidate.cache_key, redact_text(exc))
        target = compatible[0] if len(compatible) == 1 and not self.pdf_store.exists(compatible[0].cache_key) else None
        target_key = target.cache_key if target else None
        download_url = url + target.revision if target and not revision else url
        try:
            return self._read_or_download_arxiv(download_url, target_key)
        except Exception as exc:
            return [], [], None, redact_text(exc) or type(exc).__name__

    def _read_or_download_arxiv(self, url: str, target_key: str | None) -> ArxivPdfPages:
        """Parse cached source bytes or validate the final URL before caching a download."""
        pdf_id = url
        try:
            pages, evidence = _extract_pdf(self.pdf_store.read_source(url))
        except (OSError, RuntimeError, ValueError):
            data, actual_url = self.http.get_bytes_with_url(url)
            if not _compatible_arxiv_pdf(url, actual_url):
                raise ValueError("arXiv PDF response does not match the requested work or revision") from None
            pages, evidence = _extract_pdf(data)
            if target_key and not self.pdf_store.exists(target_key):
                self.pdf_store.save(target_key, data, source_url=_canonical_arxiv_pdf(actual_url))
                pdf_id = target_key
            else:
                self.pdf_store.save_source(url, data)
        _tag_pdf_occurrences(evidence, pdf_id)
        return pages, evidence, pdf_id, None

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
        *,
        include_crawler: bool = True,
        reread: set[str] | None = None,
    ) -> PdfPages:
        """Read primary and cached merged-version PDFs, preserving source-record IDs."""
        primary_id = next(
            (record.key for record in pub.source_records if record.pdf_urls or record.doi),
            pub.id,
        )
        version_ids = dict.fromkeys([
            *(pub.source_keys if pub.schema_version >= 2 else []),
            *pub.merged_ids,
            *(version.openalex_id for version in pub.versions),
        ])
        cached_versions = []
        seen_files = (
            {self.pdf_store.cache_identity(primary_id)}
            if self.pdf_store.exists(primary_id)
            else set()
        )
        for pdf_id in version_ids:
            if pdf_id == primary_id or not self.pdf_store.exists(pdf_id):
                continue
            cache_identity = self.pdf_store.cache_identity(pdf_id)
            if cache_identity in seen_files:
                continue
            seen_files.add(cache_identity)
            cached_versions.append(pdf_id)
        # An already cached version makes a new download of a missing survivor unnecessary.
        if cached_versions and not self.pdf_store.exists(primary_id):
            pages, occurrences, error = [], [], None
        else:
            pages, occurrences, error = self._primary_pdf_pages(
                pub, primary_id, include_crawler=include_crawler, reread=reread,
            )
        self.parsed_pdf_ids = {primary_id} if pages else set()
        self.failed_pdf_ids = {primary_id} if error else set()
        _tag_pdf_occurrences(occurrences, primary_id)
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
            if reread is not None:
                reread.add(pdf_id)
            _tag_pdf_occurrences(version_occurrences, pdf_id)
            pages.extend(version_pages)
            occurrences.extend(version_occurrences)
        # A canonical journal record can have only a DOI (and the crawler
        # can be unavailable) while a lower-ranked preprint record owns the
        # usable direct PDF. Provenance requires downloading that PDF under
        # the preprint's source-record key, not under the aggregate id.
        if not pages:
            fallback_ids = [
                record.key for record in pub.source_records
                if record.key != primary_id
                and record.key not in cached_versions
                and (record.pdf_urls or record.doi)
            ]
            for record_key in fallback_ids:
                fallback_pages, fallback_occurrences, fallback_error = self._primary_pdf_pages(
                    pub, record_key, include_crawler=include_crawler, reread=reread,
                )
                if fallback_pages:
                    pages.extend(fallback_pages)
                    occurrences.extend(fallback_occurrences)
                    self.parsed_pdf_ids.add(record_key)
                    _tag_pdf_occurrences(fallback_occurrences, record_key)
                    # Failed higher-ranked records are equivalent to failed
                    # URL candidates inside one record once a usable source
                    # record succeeds.
                    errors = []
                    break
                if fallback_error:
                    self.failed_pdf_ids.add(record_key)
                    errors.append(fallback_error)
        if cached_versions and not pages and not self.pdf_store.exists(primary_id):
            # Broken version caches must not prevent recovery from the primary sources.
            primary_pages, primary_occurrences, primary_error = self._primary_pdf_pages(
                pub, primary_id, include_crawler=include_crawler, reread=reread,
            )
            if primary_error:
                self.failed_pdf_ids.add(primary_id)
                errors.append(primary_error)
            if primary_pages:
                self.parsed_pdf_ids.add(primary_id)
                _tag_pdf_occurrences(primary_occurrences, primary_id)
                pages.extend(primary_pages)
                occurrences.extend(primary_occurrences)
        return pages, occurrences, "; ".join(errors) or None

    def _primary_pdf_pages(
        self,
        pub: Publication,
        record_key: str,
        *,
        include_crawler: bool = True,
        reread: set[str] | None = None,
    ) -> PdfPages:
        """Download (if not already cached) and extract per-page text + link occurrences.

        Tries every direct PDF candidate before falling back to the
        PDF-Crawler-Service (resolves a PDF from the DOI) when configured and
        reachable.

        Returns:
            (pages, page_occurrences, error). On failure both lists are empty
            and error carries the reason (e.g. a 403 for a non-open-access PDF).
        """
        errors = []
        if self.pdf_store.exists(record_key):
            try:
                pages, occurrences = _extract_pdf(self.pdf_store.read(record_key))
                if reread is not None:
                    reread.add(record_key)
                return pages, occurrences, None
            except Exception as exc:
                errors.append(redact_text(exc))

        source_record = next((record for record in pub.source_records if record.key == record_key), None)
        raw_pdf_urls = source_record.pdf_urls if source_record else pub.pdf_urls
        doi = source_record.doi if source_record else pub.doi
        supplemental = {source.url for source in pub.arxiv_sources}
        identities = {arxiv_pdf_identity(url) for url in supplemental} - {None}
        sources = [
            (url, {})
            for url in raw_pdf_urls
            if url not in supplemental and (not identities or arxiv_pdf_identity(url) not in identities)
        ]
        if not raw_pdf_urls and (doi_id := arxiv_doi_identity(doi or "")):
            native_url = f"https://arxiv.org/pdf/{''.join(doi_id)}"
            if native_url not in supplemental:
                sources.append((native_url, {}))
        elif source_record is None and (native_url := native_arxiv_pdf_url(pub)) and native_url not in supplemental:
            sources.append((native_url, {}))
        if include_crawler:
            sources.extend(self._crawler_sources(doi))
        pages, occurrences, error = self._download_primary_pdf(record_key, sources)
        if pages:
            if reread is not None:
                reread.add(record_key)
            return pages, occurrences, None
        return [], [], "; ".join(filter(None, (*errors, error))) or None

    def _crawler_sources(self, doi: str | None) -> list[tuple[str, dict[str, int]]]:
        """Build the DOI crawler candidate when the service is available."""
        if not self.crawler_available or not doi:
            return []
        return [(
            f"{self.config.pdf_crawler_url}/download?" + urlencode({"url": doi}),
            {"timeout": CRAWLER_DOWNLOAD_TIMEOUT, "retries": 0},
        )]

    def _download_primary_pdf(self, record_key: str, sources: Iterable[tuple[str, dict[str, int]]]) -> PdfPages:
        """Try the supplied sources in order and cache only successfully parsed PDFs."""
        errors = []
        for source_url, kwargs in sources:
            try:
                provenance = None
                if arxiv_pdf_identity(source_url):
                    pdf_bytes, resolved_url = self.http.get_bytes_with_url(source_url, **kwargs)
                    if not _compatible_arxiv_pdf(source_url, resolved_url):
                        raise ValueError("arXiv PDF response does not match the requested work or revision")
                    provenance = _canonical_arxiv_pdf(resolved_url)
                else:
                    pdf_bytes = self.http.get_bytes(source_url, **kwargs)
                pages, occurrences = _extract_pdf(pdf_bytes)
                # Error pages must never poison the cache for subsequent attempts.
                self.pdf_store.save(record_key, pdf_bytes, source_url=provenance)
                return pages, occurrences, None
            except Exception as exc:
                errors.append(redact_text(exc))
        return [], [], "; ".join(errors) or None
