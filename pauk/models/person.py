from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .processing import ProcessingState
from .relations import Authorship, Contribution


class Person(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    openalex_id: str | None = None
    orcid: str | None = None
    is_itmo: bool
    name_en: str | None = None
    name_variants: list[str] = Field(default_factory=list)
    email: str | None = None
    first_name_ru: str | None = None
    second_name_ru: str | None = None
    surname_ru: str | None = None
    degree: str | None = None
    github: str | None = None
    google_scholar: str | None = None
    openreview: str | None = None
    thesis: str | None = None
    department_ids: list[str] = Field(default_factory=list)
    merged_ids: list[str] = Field(default_factory=list)
    authored: list[Authorship] = Field(default_factory=list)
    contributed_to: list[Contribution] = Field(default_factory=list)
    processing: dict[str, ProcessingState] = Field(default_factory=dict, alias="_processing")

    # stub
    scopus_id: str | None = None
    researcher_id: str | None = None
    dblp_id: str | None = None
    name_ru: str | None = None
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
