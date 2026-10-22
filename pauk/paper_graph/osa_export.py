"""Reading OSA paper artifacts from disk into a `PaperGraph`.

OSA (github.com/ITMO-NSS-team/Open-Source-Advisor) writes a paper's
analysis as a directory of JSON/Markdown files: sections parsed out of the
Marker Markdown, claims extracted per section, and - where its repository
validation ran - the source files each claim is implemented in. Nothing
here calls OSA or a model: it reads what a finished OSA run left behind.

Accepted layouts under the paper-graphs directory, one publication each:

    <publication_id>/                 an OSA export directory
        claims.json | claims_legacy.json    required
        sections.json                       optional
        document.md                         optional, sections parsed from
                                            its headings when there is no
                                            sections.json
        experiments.json                    optional, claim -> repo files
        layout.json | report.json |         optional, the parsed layout of the
        elements.json                       PDF: locates a claim's sentence on
                                            a page
        graph.json                          optional, the document graph, for
                                            the figures and tables that
                                            sentence points at
        meta.json                           optional, title/repository_url
    <publication_id>.json             a PaperGraph written out verbatim

The directory (or file) name is the publication id - the OpenAlex work id,
because that is the key the map indexes publications by; `meta.json` can
override it for a directory named after something else.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from pauk.models import (
    ClaimFile,
    ClaimSource,
    DocEdge,
    DocNode,
    DocumentGraph,
    PaperClaim,
    PaperGraph,
    PaperSection,
)

logger = logging.getLogger(__name__)

CLAIMS_FILES = ("claims.json", "claims_legacy.json")
SECTIONS_FILE = "sections.json"
DOCUMENT_FILE = "document.md"
EXPERIMENTS_FILE = "experiments.json"
META_FILE = "meta.json"
DOC_GRAPH_FILE = "graph.json"
# One layout report under any of the names OSA's paper parsing writes it as.
LAYOUT_FILES = ("layout.json", "report.json", "elements.json")
# Enough of a sentence to find the element it sits in, short enough to
# survive the paragraph being broken across two elements.
MATCH_PREFIX = 80
MAX_REFERENCES = 3
CAPTION_CHARS = 200
# How far from a claim's origin the map can step through the document
# graph. Every paper ships in one map file, so the whole parsed PDF does
# not come along - just the neighbourhood a reader walks from a claim.
DOC_HOPS = 2
DOC_TEXT_CHARS = 400
# Page structure says nothing about how two elements relate: every element
# is on some page, and pages follow each other.
DOC_EDGE_TYPES = ("reading_order", "section_member", "caption_of", "text_references")

_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MARKUP = re.compile(r"[*_#`~]")
_NUMBERING = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.)]?\s+")
_LINE_SUFFIX = re.compile(r"^(.*?):(\d+)(?:-\d+)?$")


def _norm(text: str | None) -> str:
    """Whitespace- and case-insensitive key for joining rows by their text."""
    return " ".join((text or "").split()).casefold()


def parse_markdown_sections(markdown: str) -> list[PaperSection]:
    """Sections from the headings of OSA's `document.md`.

    Mirrors OSA's own `MarkdownSectionParser` closely enough that section
    ids line up (`s001`, `s002`, ... in heading order), so claims exported
    without a section id still find their section by name. Fenced code is
    skipped - a `#` comment inside a listing is not a heading.
    """
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[PaperSection] = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if not match:
            continue
        raw = match.group(2).strip()
        cleaned = _MARKUP.sub("", raw)
        numbering_match = _NUMBERING.match(cleaned)
        numbering = numbering_match.group(1) if numbering_match else None
        cleaned = " ".join(_NUMBERING.sub("", cleaned).split())
        if not cleaned:
            continue
        sections.append(
            PaperSection(
                section_id=f"s{len(sections) + 1:03d}",
                name=cleaned,
                order=len(sections) + 1,
                level=len(match.group(1)),
                numbering=numbering,
            )
        )
    return sections


def _sections_from_json(rows: list[dict]) -> list[PaperSection]:
    """Sections from `sections.json`.

    OSA drops `section_id` when it writes that file, so ids are rebuilt
    from the order - the same order OSA numbered them in.
    """
    sections = []
    for index, row in enumerate(rows, start=1):
        name = " ".join(str(row.get("name") or "").split())
        if not name:
            continue
        heading = row.get("heading_meta") or {}
        sections.append(
            PaperSection(
                section_id=str(row.get("section_id") or f"s{index:03d}"),
                name=name,
                order=index,
                level=int(heading.get("level") or 1),
                numbering=heading.get("numbering"),
            )
        )
    return sections


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def _rows_of(payload, *keys: str) -> list[dict]:
    """The rows of an export, whether it is a bare list or wrapped in a key.

    Every OSA artifact is one list of dicts written one of two ways, so the
    unwrapping is the same everywhere - only the key it hides behind differs.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next((payload[key] for key in keys if payload.get(key)), [])
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def file_url(repository_url: str | None, path: str) -> str | None:
    """A GitHub blob URL for a file an analyser named, when one can be built.

    The analyser reports whatever path it walked, which may be absolute and
    may carry a `:line` suffix; both are normalized away here. Anything
    that is not a GitHub repository gets no URL rather than a guessed one -
    other hosts spell the blob path differently.
    """
    if not repository_url or "github.com" not in repository_url:
        return None
    clean = re.sub(r"^(?:\./)+", "", path.strip().replace("\\", "/")).lstrip("/")
    if not clean:
        return None
    repo_name = repository_url.rstrip("/").rsplit("/", 1)[-1]
    if repo_name and f"{repo_name}/" in clean:
        clean = clean.split(f"{repo_name}/", 1)[1]
    anchor = ""
    line_match = _LINE_SUFFIX.match(clean)
    if line_match:
        clean, anchor = line_match.group(1), f"#L{line_match.group(2)}"
    return f"{repository_url.rstrip('/')}/blob/HEAD/{quote(clean, safe='/')}{anchor}"


def _files_of(row: dict, repository_url: str | None) -> list[ClaimFile]:
    raw = row.get("files") or row.get("impl_src_path") or []
    if isinstance(raw, str):
        raw = [raw]
    files = []
    for item in raw:
        path = item.get("path") if isinstance(item, dict) else item
        path = str(path or "").strip()
        if not path:
            continue
        url = item.get("url") if isinstance(item, dict) else None
        files.append(ClaimFile(path=path, url=url or file_url(repository_url, path)))
    return files


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in (value or []) if str(v).strip()]


def _claims_from_rows(rows: list[dict], sections: list[PaperSection],
                      repository_url: str | None) -> list[PaperClaim]:
    by_id = {s.section_id for s in sections}
    by_name = {_norm(s.name): s.section_id for s in sections}
    claims = []
    for index, row in enumerate(rows, start=1):
        text = " ".join(str(row.get("claim") or "").split())
        if not text:
            continue
        section_id = row.get("section_id")
        if section_id not in by_id:
            # Legacy exports carry the section's name, not its id.
            section_id = by_name.get(_norm(row.get("section_name"))) or by_name.get(
                _norm(row.get("section_heading_raw"))
            )
        claims.append(
            PaperClaim(
                claim_id=str(row.get("claim_id") or f"c{index:03d}"),
                claim=text,
                original_text=row.get("original_text"),
                category=row.get("category"),
                value=row.get("value"),
                verifiability=row.get("verifiability"),
                section_id=section_id,
                contradiction=bool(row.get("contradiction")),
                files=_files_of(row, repository_url),
                correspondence_percent=row.get("correspondence_percent"),
                missing=_as_list(row.get("missing")),
                reasoning=row.get("reasoning"),
            )
        )
    return claims


def _attach_experiments(claims: list[PaperClaim], rows: list[dict],
                        repository_url: str | None) -> list[PaperClaim]:
    """Fold OSA's repository validation into the claims.

    An `Experiment` is a statement taken from the paper plus the files that
    implement it, so one whose description matches an extracted claim adds
    its files to that claim. One that matches nothing is a claim of its own
    - dropping it would lose the only rows that carry file links.
    """
    by_text: dict[str, PaperClaim] = {}
    for claim in claims:
        by_text.setdefault(_norm(claim.claim), claim)
        if claim.original_text:
            by_text.setdefault(_norm(claim.original_text), claim)

    for index, row in enumerate(rows, start=1):
        description = " ".join(str(row.get("description_from_paper") or "").split())
        if not description:
            continue
        files = _files_of(row, repository_url)
        missing = _as_list(row.get("missing"))
        percent = row.get("correspondence_percent")
        reasoning = row.get("reasoning")
        claim = by_text.get(_norm(description))
        if claim is None:
            claims.append(
                PaperClaim(
                    claim_id=f"e{index:03d}",
                    claim=description,
                    section_id=None,
                    files=files,
                    correspondence_percent=percent,
                    missing=missing,
                    reasoning=reasoning,
                )
            )
            continue
        known = {f.path for f in claim.files}
        claim.files.extend(f for f in files if f.path not in known)
        if percent is not None:
            claim.correspondence_percent = percent
        if missing:
            claim.missing = missing
        if reasoning:
            claim.reasoning = reasoning
    return claims


# ---------- where a claim came from ----------


def _element_id(row: dict) -> str:
    return str(row.get("node_id") or f"elem_{row.get('bbox_idx')}")


def _element_text(row: dict) -> str:
    return " ".join(f"{row.get('text') or ''} {row.get('description') or ''}".split())


def _page_of(row: dict) -> int | None:
    """The element's page, 1-based.

    OSA numbers pages from zero (`page_0` is the first page); a reader
    counts from one, and the number is only ever displayed.
    """
    page = row.get("page_num")
    return int(page) + 1 if isinstance(page, int) else None


def _load_layout_elements(directory: Path) -> list[dict]:
    for name in LAYOUT_FILES:
        path = directory / name
        if not path.is_file():
            continue
        return _rows_of(_read_json(path), "elements", "report")
    return []


def _read_doc_graph(directory: Path) -> dict:
    path = directory / DOC_GRAPH_FILE
    if not path.is_file():
        return {}
    payload = _read_json(path)
    return payload if isinstance(payload, dict) else {}


def _doc_graph_index(payload: dict) -> dict:
    """Which figures an element points at, and what captions they carry."""
    if not payload:
        return {}
    nodes = {n["node_id"]: n for n in payload.get("nodes", [])
             if isinstance(n, dict) and n.get("node_id")}
    references, captions = defaultdict(list), defaultdict(list)
    for edge in payload.get("edges", []):
        if not isinstance(edge, dict):
            continue
        if edge.get("edge_type") == "text_references":
            references[edge.get("source")].append(edge)
        elif edge.get("edge_type") == "caption_of":
            captions[edge.get("target")].append(edge.get("source"))
    return {"nodes": nodes, "references": references, "captions": captions}


def _document_graph(payload: dict, elements: list[dict],
                     claims: list[PaperClaim]) -> DocumentGraph | None:
    """The document graph within `DOC_HOPS` of any claim's origin."""
    edges = [e for e in payload.get("edges", [])
             if isinstance(e, dict) and e.get("edge_type") in DOC_EDGE_TYPES
             and e.get("source") and e.get("target") and e.get("source") != e.get("target")]
    origins = {s.source_id for c in claims for s in c.sources if s.kind != "quote"}
    if not edges or not origins:
        return None
    around = defaultdict(set)
    for edge in edges:
        around[edge["source"]].add(edge["target"])
        around[edge["target"]].add(edge["source"])
    keep, frontier = set(origins), set(origins)
    for _ in range(DOC_HOPS):
        frontier = {n for node in frontier for n in around[node]} - keep
        keep |= frontier

    # The layout pass may carry text the graph's own nodes lack, or the
    # other way round; either one is better than an empty box.
    by_id = {_element_id(row): row for row in elements}
    graph_nodes = {n["node_id"]: n for n in payload.get("nodes", [])
                   if isinstance(n, dict) and n.get("node_id")}
    nodes = []
    for node_id in sorted(keep):
        row = graph_nodes.get(node_id) or by_id.get(node_id)
        if row is None:
            continue
        text = str(row.get("text") or "").strip() or _element_text(by_id.get(node_id, {})) \
            or str(row.get("description") or "").strip()
        text = " ".join(text.split())
        nodes.append(DocNode(
            node_id=node_id,
            name=row.get("name"),
            page=_page_of(row),
            text=text[:DOC_TEXT_CHARS] or None,
        ))
    kept = {n.node_id for n in nodes}
    return DocumentGraph(
        nodes=nodes,
        edges=[
            DocEdge(source=e["source"], target=e["target"], edge_type=e["edge_type"],
                    label=e.get("matched_pattern"))
            for e in edges if e["source"] in kept and e["target"] in kept
        ],
    )


def _match_element(quote: str, elements: list[tuple[str, dict]]) -> dict | None:
    """The layout element a claim's evidence sentence sits in.

    Substring on normalized text, because the sentence in `claims.json`
    came from Marker's Markdown while the element text came from the
    layout pass: the same words, not the same whitespace or line breaks.
    Falls back to the opening of the sentence, which is what survives when
    a paragraph is split across two elements.
    """
    needle = _norm(quote)
    if not needle:
        return None
    for probe in (needle, needle[:MATCH_PREFIX]):
        if len(probe) < 12:
            continue
        for text, row in elements:
            if probe in text:
                return row
    return None


def _caption_text(target_id: str, index: dict, by_id: dict[str, dict]) -> str | None:
    for caption_id in index["captions"].get(target_id, []):
        text = _element_text(by_id.get(caption_id, {}))
        if text:
            return text[:CAPTION_CHARS]
    return None


def _reference_sources(element_id: str, index: dict, by_id: dict[str, dict]) -> list[ClaimSource]:
    sources = []
    for edge in index["references"].get(element_id, [])[:MAX_REFERENCES]:
        target_id = edge.get("target")
        node = index["nodes"].get(target_id) or by_id.get(target_id) or {}
        label = edge.get("matched_pattern") or node.get("name") or edge.get("reference_category")
        sources.append(
            ClaimSource(
                source_id=str(target_id),
                kind="reference",
                text=_caption_text(target_id, index, by_id),
                page=_page_of(node),
                label=str(label) if label else None,
            )
        )
    return sources


def _attach_sources(claims: list[PaperClaim], sections: list[PaperSection],
                    elements: list[dict], index: dict) -> None:
    """Give every claim the nodes it was extracted from.

    The sentence itself is always one, with or without a layout export -
    it is the evidence the claim stands on. The element and the figures it
    references are the parts only a parsed PDF can say.
    """
    section_name = {s.section_id: s.name for s in sections}
    by_id = {_element_id(row): row for row in elements}
    # Both sides normalized the same way, or the match is decided by
    # whitespace and capitalisation rather than by the words.
    searchable = [(_norm(_element_text(row)), row) for row in elements]
    searchable = [(text, row) for text, row in searchable if text]
    for claim in claims:
        quote = (claim.original_text or "").strip()
        if not quote:
            continue
        match = _match_element(quote, searchable) if searchable else None
        page = _page_of(match) if match else None
        sources = [
            ClaimSource(
                source_id=f"{claim.claim_id}:quote",
                kind="quote",
                text=quote,
                page=page,
                label=section_name.get(claim.section_id),
            )
        ]
        if match:
            element_id = _element_id(match)
            sources.append(
                ClaimSource(
                    source_id=element_id,
                    kind="element",
                    page=page,
                    label=str(match.get("name") or "element"),
                )
            )
            if index:
                sources.extend(_reference_sources(element_id, index, by_id))
        claim.sources = sources


def load_export(directory: Path, publication_id: str | None = None) -> PaperGraph | None:
    """One OSA export directory as a `PaperGraph`, or None if it has no claims."""
    meta = {}
    meta_path = directory / META_FILE
    if meta_path.is_file():
        loaded = _read_json(meta_path)
        meta = loaded if isinstance(loaded, dict) else {}

    claims_payload = None
    for name in CLAIMS_FILES:
        path = directory / name
        if path.is_file():
            claims_payload = _read_json(path)
            break
    if claims_payload is None:
        logger.info("%s: no %s, skipped", directory, " / ".join(CLAIMS_FILES))
        return None

    sections_path = directory / SECTIONS_FILE
    document_path = directory / DOCUMENT_FILE
    if sections_path.is_file():
        rows = _read_json(sections_path)
        sections = _sections_from_json(rows if isinstance(rows, list) else [])
    elif document_path.is_file():
        sections = parse_markdown_sections(document_path.read_text(encoding="utf-8"))
    else:
        sections = []

    repository_url = meta.get("repository_url") or meta.get("repo_url")
    claims = _claims_from_rows(_rows_of(claims_payload, "claims", "result"), sections, repository_url)

    experiments_path = directory / EXPERIMENTS_FILE
    if experiments_path.is_file():
        claims = _attach_experiments(
            claims,
            _rows_of(_read_json(experiments_path), "experiments", "result"),
            repository_url,
        )
    if not claims:
        logger.info("%s: claims file holds no usable claim, skipped", directory)
        return None

    elements = _load_layout_elements(directory)
    doc_payload = _read_doc_graph(directory)
    _attach_sources(claims, sections, elements, _doc_graph_index(doc_payload))

    used = {claim.section_id for claim in claims}
    return PaperGraph(
        publication_id=str(meta.get("publication_id") or publication_id or directory.name),
        title=meta.get("title"),
        repository_url=repository_url,
        source=str(meta.get("source") or "osa"),
        # Sections nothing was extracted from would draw a wide empty fan
        # on the map; the paper's text is not lost, it is just not a node.
        sections=[s for s in sections if s.section_id in used],
        claims=claims,
        document=_document_graph(doc_payload, elements, claims) if doc_payload else None,
    )


def load_paper_graphs(root: Path) -> list[PaperGraph]:
    """Every paper graph under `root`, ordered by publication id.

    A file or directory that cannot be read is logged and skipped: one bad
    export must not cost the map every other paper.
    """
    if not root.is_dir():
        logger.info("Paper graphs: %s does not exist, nothing to read", root)
        return []
    graphs: dict[str, PaperGraph] = {}
    for entry in sorted(root.iterdir()):
        try:
            if entry.is_dir():
                graph = load_export(entry)
            elif entry.suffix == ".json":
                graph = PaperGraph.model_validate(_read_json(entry))
            else:
                continue
        except Exception as exc:
            logger.warning("Paper graph %s skipped: %s: %s", entry, type(exc).__name__, exc)
            continue
        if graph is None:
            continue
        if graph.publication_id in graphs:
            logger.warning("Paper graph %s: duplicate publication id, overwritten", entry)
        graphs[graph.publication_id] = graph
    return [graphs[key] for key in sorted(graphs)]
