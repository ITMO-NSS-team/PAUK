from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .processing import ProcessingState
from .relations import MentionsLink


class Funding(BaseModel):
    funder: str | None = None
    grant_id: str | None = None


class Publication(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str
    journal: str | None = None
    doi: str | None = None
    publication_date: date | None = None
    year: int | None = None
    has_code: bool = False
    code_url: str | None = None
    funding: list[Funding] = Field(default_factory=list)
    openalex_url: str | None = None
    pdf_url: str | None = None
    abstract: str | None = None
    department_ids: list[str] = Field(default_factory=list)
    mentions_links: list[MentionsLink] = Field(default_factory=list)
    processing: dict[str, ProcessingState] = Field(default_factory=dict, alias="_processing")
