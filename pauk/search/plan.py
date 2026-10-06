"""The structured plan LLM №1 extracts from a question.

Values are English codes so the code can branch on them reliably; the UI
shows them in Russian.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EntityKind = Literal["person", "department"]
Section = Literal["persons", "departments", "publications", "repositories", "counts"]
GraphType = Literal["topic_subgraph", "person_profile", "department_profile"]
QueryType = Literal["topic", "graph", "mixed"]

SECTIONS: tuple[str, ...] = ("persons", "departments", "publications", "repositories", "counts")

# Which fields of each section can be asked for; anything else is ignored
# rather than trusted, since the model invents names.
FIELDS: dict[str, tuple[str, ...]] = {
    "persons": ("name", "name_ru", "name_en", "orcid", "departments", "publications", "repositories", "score"),
    "departments": ("name", "kind", "publications", "persons", "score"),
    "publications": ("title", "year", "journal", "authors", "score"),
    "repositories": ("name", "url", "stars", "description", "contributors", "score"),
}


# Words that fit any scientific text. A core phrase made only of them pulls
# search towards whatever says "architecture" (network diagrams, buildings),
# and the model adds them despite being told not to, so they are dropped here.
GENERIC_WORDS = {
    "архитектура", "архитектуры", "архитектурами", "architecture", "architectures",
    "метод", "методы", "методами", "method", "methods", "подход", "подходы", "approach", "approaches",
    "модель", "модели", "моделями", "model", "models", "система", "системы", "system", "systems",
    "исследование", "исследования", "research", "study", "studies", "применение", "application", "applications",
    "алгоритм", "алгоритмы", "algorithm", "algorithms", "технология", "технологии", "technology", "technologies",
}


def _is_generic(phrase: str) -> bool:
    words = phrase.casefold().replace("-", " ").split()
    return bool(words) and all(word in GENERIC_WORDS for word in words)


class Entity(BaseModel):
    kind: EntityKind
    name: str = Field(min_length=1)


class Filters(BaseModel):
    only_itmo: bool = True
    year_from: int | None = None
    year_to: int | None = None


class QueryPlan(BaseModel):
    type: QueryType = "topic"
    entities: list[Entity] = Field(default_factory=list)
    expected_values: list[Section] = Field(default_factory=lambda: ["persons", "departments", "publications",
                                                                    "repositories"])
    fields: list[str] = Field(default_factory=list)
    filter: Filters = Field(default_factory=Filters)
    core: list[str] = Field(default_factory=list)
    graph_type: GraphType = "topic_subgraph"

    @field_validator("core")
    @classmethod
    def _clean_core(cls, value: list[str]) -> list[str]:
        phrases = list(dict.fromkeys(phrase.strip() for phrase in value if phrase and phrase.strip()))
        # A question that is about "methods" and nothing else keeps them: an
        # empty core would turn a topic question into no search at all.
        return [phrase for phrase in phrases if not _is_generic(phrase)] or phrases

    @field_validator("expected_values")
    @classmethod
    def _dedupe_sections(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value)) or list(SECTIONS[:4])

    @model_validator(mode="after")
    def _consistent(self) -> QueryPlan:
        # The type follows from what was found, whatever the model labelled
        # it: a topic needs core phrases, a graph question needs an entity.
        if self.entities and self.core:
            self.type = "mixed"
        elif self.entities:
            self.type = "graph"
        else:
            self.type = "topic"
        if self.type == "topic":
            self.graph_type = "topic_subgraph"
        elif self.type == "graph" and self.graph_type == "topic_subgraph":
            self.graph_type = "person_profile" if self.entities[0].kind == "person" else "department_profile"
        return self

    def wanted_fields(self, section: str) -> set[str] | None:
        allowed = set(FIELDS.get(section, ()))
        chosen = {name for name in self.fields if name in allowed}
        return chosen or None


def fallback_plan(question: str) -> QueryPlan:
    """Used when the model is unavailable or answers garbage: treat it as a topic."""
    return QueryPlan(type="topic", core=[question])
