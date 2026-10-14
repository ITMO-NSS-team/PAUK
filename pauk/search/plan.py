"""The structured plan LLM №1 extracts from a question.

Values are English codes so the code can branch on them reliably; the UI
shows them in Russian.
"""

from __future__ import annotations
from datetime import date

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EntityKind = Literal["person", "department"]
EntityRole = Literal["subject", "partner"]
Section = Literal["persons", "departments", "publications", "repositories", "counts"]
GraphType = Literal["topic_subgraph", "person_profile", "department_profile"]
QueryType = Literal["topic", "graph", "mixed"]
TemplateId = Literal["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "none"]
Subtype = Literal["3a", "3b", "3c", "3d", "3e"]

SECTIONS: tuple[str, ...] = ("persons", "departments", "publications", "repositories", "counts")
TEMPLATE_IDS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10")
TARGETS: tuple[str, ...] = ("persons", "publications", "repositories")

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
    role: EntityRole | None = None


class Filters(BaseModel):
    only_itmo: bool = True
    year_from: int | None = None
    year_to: int | None = None


@dataclass(frozen=True)
class EntitySlots:
    """Entities placed into the parameter slots of the labelled `params`."""

    person_ids: list[Entity]
    dept_id: Entity | None
    partner_dept_id: Entity | None

def _current_year() -> int:
    """Separate function so tests can pin "today"."""
    return date.today().year
class QueryPlan(BaseModel):
    type: QueryType = "topic"
    entities: list[Entity] = Field(default_factory=list)
    expected_values: list[Section] = Field(default_factory=lambda: ["persons", "departments", "publications",
                                                                    "repositories"])
    fields: list[str] = Field(default_factory=list)
    filter: Filters = Field(default_factory=Filters)
    core: list[str] = Field(default_factory=list)
    graph_type: GraphType = "topic_subgraph"

    template: TemplateId = "none"
    subtype: Subtype | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("template", mode="before")
    @classmethod
    def _normalise_template(cls, value: Any) -> str:
        # A number, a padded string or an unknown id from the model must not
        # sink the whole plan: whatever is not a template falls back to "none".
        text = str(value).strip().lower() if value is not None else "none"
        return text if text in TEMPLATE_IDS else "none"

    def model_dump(self, **kwargs) -> dict[str, any]:
        return super().model_dump(**kwargs)

    @field_validator("subtype", mode="before")
    @classmethod
    def _ignore_subtype(cls, value: Any) -> None:
        return None

    @field_validator("params", mode="before")
    @classmethod
    def _clean_params(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        # Models write null for "not given"; an absent key is the contract.
        return {key: item for key, item in value.items() if item is not None}

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
        self._apply_template()
        return self

    def _apply_template(self) -> None:
        if self.template == "none":
            self.subtype = None
            return
        self.subtype = self._derive_subtype(self.template, self.entity_slots())

        # What the answer contains follows from the parameters, not from the
        # model's guess: a list returns rows of the target, anything else a count.
        target = self.params.get("target")
        metric = self.params.get("metric", "list")
        if metric == "list" and not self.params.get("group_by"):
            if target in TARGETS:
                self.expected_values = [target]
        else:
            self.expected_values = ["counts"]
        years = self.params.get("last_n_years")
        if (isinstance(years, int) and not isinstance(years, bool) and years > 0
                and self.filter.year_from is None and self.filter.year_to is None):
            self.filter.year_from = self._current_year() - years + 1

    def entity_slots(self) -> EntitySlots:
        persons = [e for e in self.entities if e.kind == "person"]
        departments = [e for e in self.entities if e.kind == "department"]
        if persons:
            return EntitySlots(persons, None, departments[0] if departments else None)
        subject = next((e for e in departments if e.role == "subject"), None)
        partner = next((e for e in departments if e.role == "partner"), None)
        free = [e for e in departments if e.role is None]
        if subject is None and free:
            subject = free.pop(0)
        if partner is None and free:
            partner = free.pop(0)
        return EntitySlots([], subject, partner)

    def wanted_fields(self, section: str) -> set[str] | None:
        allowed = set(FIELDS.get(section, ()))
        chosen = {name for name in self.fields if name in allowed}
        return chosen or None

    def _derive_subtype(template: str, slots: EntitySlots) -> str | None:
        """Template 3 splits by who the question is about and who it compares with."""
        if template != "3":
            return None
        people = len(slots.person_ids)
        subject, partner = slots.dept_id is not None, slots.partner_dept_id is not None
        if 2 <= people <= 3 and not subject and not partner:
            return "3a"
        if people == 1 and not subject and not partner:
            return "3b"
        if people == 0 and subject and partner:
            return "3c"
        if people == 1 and partner and not subject:
            return "3d"
        if people == 0 and not partner:
            return "3e"
        return None

def fallback_plan(question: str) -> QueryPlan:
    """Used when the model is unavailable or answers garbage: treat it as a topic."""
    return QueryPlan(type="topic", core=[question])
