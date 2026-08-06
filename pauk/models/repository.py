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


class LinkOccurrence(BaseModel):
    context: str | None = None
    page_number: int | None = None


class CodeLink(BaseModel):
    url: str
    host: str | None = None
    occurrences: list[LinkOccurrence] = Field(default_factory=list)
    is_relevant: bool | None = None
    llm_confidence: float | None = None
    llm_reason: str | None = None


class Repository(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    url: str
    # GitHub's own numeric id: stable across renames and owner transfers,
    # which is what lets the dedup stage recognise rows created before a
    # rename as the same repository.
    github_id: int | None = None
    # URLs this repo was cited by before canonicalization (renames, case
    # variants) — lets the graph loader resolve old links to this node.
    cited_urls: list[str] = Field(default_factory=list)
    merged_ids: list[str] = Field(default_factory=list)
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
