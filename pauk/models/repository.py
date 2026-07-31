from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .processing import ProcessingState


class GitHubProfile(BaseModel):
    id: str
    login: str
    name: str | None = None
    html_url: str | None = None
    description: str | None = None
    location: str | None = None
    type: str | None = None


class LinkCandidate(BaseModel):
    id: str
    url: str
    host: str | None = None


class CodeLink(BaseModel):
    url: str
    host: str | None = None
    context: str | None = None
    page_number: int | None = None
    is_relevant: bool | None = None
    llm_confidence: float | None = None
    llm_reason: str | None = None


class Repository(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    url: str
    description: str | None = None
    access_date: date | None = None
    has_readme: bool = False
    stars_num: int | None = None
    last_updated: date | None = None
    license: str | None = None
    contributors: list[str] = Field(default_factory=list)
    owner_login: str | None = None
    department_ids: list[str] = Field(default_factory=list)
    publication_ids: list[str] = Field(default_factory=list)
    processing: dict[str, ProcessingState] = Field(default_factory=dict, alias="_processing")


class RepoLink(BaseModel):
    publication_id: str
    links: list[CodeLink] = Field(default_factory=list)
