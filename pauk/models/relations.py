from typing import Literal

from pydantic import BaseModel


class Authorship(BaseModel):
    publication_id: str
    position: int | None = None
    affiliation: str | None = None
    # None while the affiliation is the one the work itself states; set when
    # it had none and the persons stage filled it from the author's own
    # records ("openalex" / "orcid").
    affiliation_source: str | None = None
    is_corresponding: bool = False


class Contribution(BaseModel):
    repository_id: str
    role: str | None = None


class MentionsLink(BaseModel):
    target_kind: Literal["repository", "candidate"]
    repository_url: str | None = None
    candidate_id: str | None = None
    context: str | None = None
    page_number: int | None = None
    is_relevant: bool | None = None
    llm_confidence: float | None = None
    llm_reason: str | None = None

