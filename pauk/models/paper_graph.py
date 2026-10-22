from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimFile(BaseModel):
    """A repository file that implements a claim.

    `path` is kept exactly as the analyser reported it, so a path that the
    URL builder could not make sense of is still shown to a reader; `url`
    is filled only when the repository is known and the path looks like a
    file inside it.
    """

    path: str
    url: str | None = None


class ClaimSource(BaseModel):
    """Where in the paper a claim was taken from.

    Always at least the sentence itself (`kind="quote"`). Where an export
    also carries the layout of the PDF, the element that sentence sits in
    (`kind="element"`) and the figures or tables it points at
    (`kind="reference"`) come with it.
    """

    source_id: str
    kind: str
    text: str | None = None
    # 1-based, however the exporter numbered its pages.
    page: int | None = None
    label: str | None = None


class PaperClaim(BaseModel):
    """One verifiable statement extracted from a section of a paper.

    Fields mirror OSA's `ExtractedClaim` plus the repository side of the
    link: `files` (its implementation) and the validation numbers that come
    with it.
    """

    claim_id: str
    claim: str
    # Verbatim sentence the claim was taken from - the evidence a reader
    # checks the claim against.
    original_text: str | None = None
    category: str | None = None
    value: str | None = None
    verifiability: str | None = None
    section_id: str | None = None
    contradiction: bool = False
    files: list[ClaimFile] = Field(default_factory=list)
    sources: list[ClaimSource] = Field(default_factory=list)
    # Set only where repository validation ran: how much of the claim the
    # files cover (0-100), and what it could not find.
    correspondence_percent: float | None = None
    missing: list[str] = Field(default_factory=list)
    reasoning: str | None = None


class PaperSection(BaseModel):
    """A section of the paper - the "part of the graph" a claim comes from."""

    section_id: str
    name: str
    order: int
    level: int = 1
    numbering: str | None = None


class DocNode(BaseModel):
    """An element of the parsed PDF: a paragraph, heading, figure, caption."""

    node_id: str
    name: str | None = None
    page: int | None = None
    text: str | None = None


class DocEdge(BaseModel):
    """A structural link between two elements, as the document graph names it
    (`reading_order`, `section_member`, `caption_of`, `text_references`)."""

    source: str
    target: str
    edge_type: str
    label: str | None = None


class DocumentGraph(BaseModel):
    """The part of the paper's document graph around the claims' origins.

    Lets a reader step from the element a claim was taken from to what it
    sits next to - the paragraph before it, its section heading, the figure
    it cites - without the whole paper riding along in the map file.
    """

    nodes: list[DocNode] = Field(default_factory=list)
    edges: list[DocEdge] = Field(default_factory=list)


class PaperGraph(BaseModel):
    """A single paper as a graph: publication -> sections -> claims -> files."""

    publication_id: str
    title: str | None = None
    repository_url: str | None = None
    # Where the graph came from ("osa", or whatever produced the file).
    source: str | None = None
    sections: list[PaperSection] = Field(default_factory=list)
    claims: list[PaperClaim] = Field(default_factory=list)
    document: DocumentGraph | None = None
