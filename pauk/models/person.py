from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .processing import ProcessingState
from .relations import Authorship, Contribution


class Affiliation(BaseModel):
    """Where a person worked, as one source states it.

    Self-deposited records (Zenodo, SSRN) routinely omit the affiliation of
    some coauthors, leaving the authorship with nothing to place them by.
    The author's own OpenAlex record and their ORCID employments know it,
    so both are collected here — `source` keeps the two apart, and `years`
    is what lets an authorship pick the affiliation of its own year.
    """

    name: str
    ror: str | None = None
    years: list[int] = Field(default_factory=list)
    source: str


class Person(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    openalex_id: str | None = None
    orcid: str | None = None
    is_itmo: bool
    name_raw: str | None = None
    name_variants: list[str] = Field(default_factory=list)
    first_name_ru: str | None = None
    second_name_ru: str | None = None
    surname_ru: str | None = None
    first_name_en: str | None = None
    second_name_en: str | None = None
    surname_en: str | None = None
    degree: str | None = None
    email: str | None = None
    emails: list[str] = Field(default_factory=list)
    github: str | None = None
    google_scholar: str | None = None
    openreview: str | None = None
    thesis: str | None = None
    department_ids: list[str] = Field(default_factory=list)
    affiliations: list[Affiliation] = Field(default_factory=list)
    merged_ids: list[str] = Field(default_factory=list)
    authored: list[Authorship] = Field(default_factory=list)
    contributed_to: list[Contribution] = Field(default_factory=list)
    processing: dict[str, ProcessingState] = Field(default_factory=dict, alias="_processing")

    # TODO: check this stubs
    scopus_id: str | None = None
    researcher_id: str | None = None
    dblp_id: str | None = None
    name_ru: str | None = None
    name_en: str | None = None
    other_names: list[str] = Field(default_factory=list)
    biography: str | None = None
    country: str | None = None
    homepage: str | None = None
    gitlab_username: str | None = None
    linkedin: str | None = None
    twitter: str | None = None
    wikipedia: str | None = None
    works_count: int | None = None
    cited_by_count: int | None = None
    h_index: int | None = None
    i10_index: int | None = None
    counts_by_year: dict | None = None
    status: str | None = None
    created_at: datetime | None = None
    enriched_at: datetime | None = None
