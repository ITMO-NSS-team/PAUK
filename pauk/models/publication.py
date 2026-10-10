from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .processing import ProcessingState
from .relations import MentionsLink


class Funding(BaseModel):
    funder: str | None = None
    grant_id: str | None = None


class VersionAuthor(BaseModel):
    """One author of one version, as that record listed them."""

    person_id: str
    name: str | None = None
    position: int | None = None


class PublicationVersion(BaseModel):
    """One place a work appeared: a preprint, dataset deposit, journal version or duplicate record.

    The dedup stage folds such records into a single Publication and keeps
    one entry here per record, including the surviving one, so that no
    venue, DOI, abstract or author list is lost by merging. This is a
    ledger, not the graph: nodes and AUTHORED edges are always drawn from
    the merged state of the surviving publication.
    """

    openalex_id: str
    title: str | None = None
    doi: str | None = None
    journal: str | None = None
    publication_date: date | None = None
    year: int | None = None
    openalex_url: str | None = None
    pdf_urls: list[str] = Field(default_factory=list)
    abstract: str | None = None
    authors: list[VersionAuthor] = Field(default_factory=list)


class Publication(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str
    # OpenAlex work type: "article", "preprint", "software", "dataset", ...
    # Not every work is a paper (e.g. a software release archived on Zenodo),
    # and the code_links stage treats those differently.
    type: str | None = None
    # OpenAlex topic fields ("Computer Science", "Chemistry"): what the work
    # is about, coarse enough to be stable. Dedup uses an overlap between
    # two persons' fields as evidence that they are one researcher.
    fields: list[str] = Field(default_factory=list)
    journal: str | None = None
    doi: str | None = None
    publication_date: date | None = None
    year: int | None = None
    has_code: bool = False
    # JSON text with every author-produced repository URL. Kept as a string
    # because the GUI and existing Neo4j data rely on that format.
    code_url: str | None = None
    funding: list[Funding] = Field(default_factory=list)
    openalex_url: str | None = None
    pdf_urls: list[str] = Field(default_factory=list)
    abstract: str | None = None
    full_text: str | None = None
    department_ids: list[str] = Field(default_factory=list)
    mentions_links: list[MentionsLink] = Field(default_factory=list)
    # Every record folded into this publication (see PublicationVersion) and
    # the OpenAlex work ids those records were keyed by.
    versions: list[PublicationVersion] = Field(default_factory=list)
    merged_ids: list[str] = Field(default_factory=list)
    processing: dict[str, ProcessingState] = Field(default_factory=dict, alias="_processing")
