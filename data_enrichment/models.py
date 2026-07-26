from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

# --- Связи со свойствами ---

class Authorship(BaseModel):
    """Ребро (Person)-[:AUTHORED]->(Publication)."""
    publication_id: str
    position: int | None = None
    affiliation: str | None = None
    is_corresponding: bool = False


class Contribution(BaseModel):
    """Ребро (Person:Itmo)-[:CONTRIBUTED_TO]->(Repository)."""
    repository_id: str
    role: str | None = None


class MentionsLink(BaseModel):
    """Ребро (Publication)-[:MENTIONS_LINK]->(Repository | LinkCandidate).

    target_kind различает цель: сопоставленный репозиторий по url или
    несопоставленный кандидат по id.
    """
    target_kind: Literal["repository", "candidate"]
    repository_url: str | None = None   # при target_kind == "repository"
    candidate_id: str | None = None     # при target_kind == "candidate"
    context: str | None = None
    page_number: int | None = None
    is_relevant: bool | None = None
    llm_confidence: float | None = None
    llm_reason: str | None = None


# --- Узлы ---

class Department(BaseModel):
    """Узел (:Department)."""
    id: str
    name_en: str
    name_ru: str | None = None
    name_variants: list[str] = []


class GitHubProfile(BaseModel):
    """Узел (:GitHubProfile) - аккаунт или организация на GitHub."""
    id: str
    login: str
    name: str | None = None
    html_url: str | None = None
    description: str | None = None
    location: str | None = None
    type: str | None = None             # 'org' | 'user'


class LinkCandidate(BaseModel):
    """Узел (:LinkCandidate) - github ссылка из публикации, не сведённая к репозиторию."""
    id: str
    url: str
    host: str | None = None


class Repository(BaseModel):
    """Узел (:Repository)."""
    id: str
    name: str
    url: str
    description: str | None = None
    access_date: date | None = None
    has_readme: bool = False
    stars_num: int | None = None
    last_updated: date | None = None
    license: str | None = None
    contributors: list[str] = []        # свойство-массив логины контрибьюторов
    owner_login: str | None = None      # -> OWNED_BY GitHubProfile
    department_ids: list[str] = []      # -> DEVELOPED_BY
    publication_ids: list[str] = []     # -> IMPLEMENTS


class Publication(BaseModel):
    """Узел (:Publication)."""
    id: str
    title: str
    journal: str | None = None
    doi: str | None = None
    publication_date: date | None = None
    year: int | None = None
    has_code: bool = False
    code_url: str | None = None
    funding: str | None = None
    openalex_url: str | None = None
    pdf_url: str | None = None
    abstract: str | None = None
    department_ids: list[str] = []      # -> PRODUCED_BY
    mentions_links: list[MentionsLink] = []


class Person(BaseModel):
    """Общие поля меток (:Person:Itmo) и (:Person:External)."""
    id: str
    name_en: str | None = None
    name_variants: list[str] = []
    email: str | None = None
    authored: list[Authorship] = []     # -> AUTHORED


class ItmoPerson(Person):
    """Узел (:Person:Itmo) - основной объект конвейера."""
    first_name_ru: str | None = None
    second_name_ru: str | None = None
    surname_ru: str | None = None
    degree: str | None = None
    github: str | None = None
    google_scholar: str | None = None
    openreview: str | None = None
    thesis: str | None = None
    created_at: datetime | None = None
    department_ids: list[str] = []      # -> BELONGS_TO
    contributed_to: list[Contribution] = []   # -> CONTRIBUTED_TO


class ExternalPerson(Person):
    """Узел (:Person:External) - соавтор не из ИТМО."""
    # Поля те же что и у Person, отдельный класс чтобы различать