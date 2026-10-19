"""The structured plan LLM №1 extracts from a question.

Values are English codes so the code can branch on them reliably; the UI
shows them in Russian.
"""

from __future__ import annotations
from datetime import date

from dataclasses import dataclass, field
from typing import Any, Literal, Dict, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

EntityKind = Literal["person", "department"]
EntityRole = Literal["subject", "partner"]
Section = Literal["persons", "departments", "publications", "repositories", "counts"]
GraphType = Literal["topic_subgraph", "person_profile", "department_profile"]
QueryType = Literal["topic", "graph", "mixed"]
TemplateId = Literal["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "none"]
Subtype = Literal["3a", "3b", "3c", "3d", "3e", "5a", "5b", "5c"]

SECTIONS: tuple[str, ...] = ("persons", "departments", "publications", "repositories", "counts")
TEMPLATE_IDS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10")
TARGETS: tuple[str, ...] = ("persons", "publications", "repositories")

# What a template does follows from its number: 1-5 only walk the graph,
# 6-7 are a pure semantic search, 8-10 combine a search with a graph condition.
TEMPLATE_KIND: dict[str, QueryType] = {
    "1": "graph", "2": "graph", "3": "graph", "4": "graph", "5": "graph",
    "6": "topic", "7": "topic",
    "8": "mixed", "9": "mixed", "10": "mixed",
}

# Which fields of each section can be asked for; anything else is ignored
# rather than trusted, since the model invents names.
FIELDS: dict[str, tuple[str, ...]] = {
    "persons": ("name", "name_ru", "name_en", "orcid", "departments", "publications", "repositories", "score"),
    "departments": ("name", "kind", "publications", "persons", "score"),
    "publications": ("title", "year", "journal", "authors", "score"),
    "repositories": ("name", "url", "stars", "description", "contributors", "score"),
}

# Fields a condition [{"field", "op", "value"}] may use, by what it filters.
PERSON_FIELDS: tuple[str, ...] = (
    "degree", "github", "google_scholar", "linkedin", "orcid", "homepage", "other_names", "is_itmo",
)
PUBLICATION_FIELDS: tuple[str, ...] = ("type", "doi", "funding", "pdf_urls", "journal")
REPOSITORY_FIELDS: tuple[str, ...] = (
    "license", "stars_num", "forks_num", "contributors_count", "archived", "is_fork", "has_readme",
    "language", "last_updated", "owner_type", "role",
)
OPS: tuple[str, ...] = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "is_null", "not_null")
NULL_OPS: tuple[str, ...] = ("is_null", "not_null")

METRICS: tuple[str, ...] = ("list", "count", "exists", "ratio", "avg_per_year", "avg_per_person")
ORDERS: tuple[str, ...] = ("asc", "desc")
RANK_BY: tuple[str, ...] = ("distinct_coauthors", "joint_publications")
LINKS: tuple[str, ...] = ("mentions", "implements")
# The chain patterns of template 5; each one is a query of its own.
PATTERNS: dict[str, Subtype] = {
    "author_contributor": "5a",
    "author_not_contributor": "5b",
    "author_with_repo": "5c",
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


class _Unsupported(Exception):
    """The plan asks for something its template cannot express."""


@dataclass(frozen=True)
class TemplateSpec:
    """What a template accepts. Anything outside of it makes the plan "none"."""

    keys: frozenset[str]  # allowed keys of params
    targets: tuple[str, ...]
    metrics: tuple[str, ...] = ()  # empty: only "exists" may be written (templates 5-10)
    group_by: tuple[str, ...] = ()
    conditions: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    topic: bool = False  # needs core phrases for the semantic search
    need_department: bool = False
    max_persons: int = 0
    max_departments: int = 0


def _keys(*names: str) -> frozenset[str]:
    return frozenset(("target", "metric", *names))


TEMPLATE_SPECS: dict[str, TemplateSpec] = {
    "1": TemplateSpec(
        keys=_keys("filters", "group_by", "order", "limit", "no_dept"),
        targets=("persons",), metrics=("list", "count", "exists"), group_by=("dept", "degree"),
        conditions={"filters": PERSON_FIELDS}, max_departments=1),
    "2": TemplateSpec(
        keys=_keys("filters", "ratio_filters", "group_by", "order", "limit", "last_n_years"),
        targets=("publications",), metrics=METRICS, group_by=("dept", "year", "quarter", "type"),
        conditions={"filters": PUBLICATION_FIELDS, "ratio_filters": PUBLICATION_FIELDS}, max_departments=1),
    "3": TemplateSpec(
        keys=_keys("coauthor_is_itmo", "rank_by", "order", "limit", "last_n_years"),
        targets=("persons", "publications"), metrics=("list", "count", "exists"),
        max_persons=3, max_departments=2),
    "4": TemplateSpec(
        keys=_keys("filters", "group_by", "order", "limit"),
        targets=("persons", "repositories"), metrics=("list", "count", "exists"), group_by=("dept",),
        conditions={"filters": REPOSITORY_FIELDS}, max_persons=1, max_departments=1),
    "5": TemplateSpec(
        keys=_keys("pattern", "filters"),
        targets=("persons",), conditions={"filters": REPOSITORY_FIELDS}, max_departments=1),
    "6": TemplateSpec(keys=_keys(), targets=("persons",), topic=True),
    "7": TemplateSpec(keys=_keys(), targets=("publications",), topic=True),
    "8": TemplateSpec(
        keys=_keys("last_n_years"),
        targets=("persons", "publications"), topic=True, need_department=True, max_departments=1),
    "9": TemplateSpec(
        keys=_keys("require_code", "link", "repo_filters", "last_n_years"),
        targets=("persons", "publications", "repositories"), conditions={"repo_filters": REPOSITORY_FIELDS},
        topic=True, max_departments=1),
    "10": TemplateSpec(
        keys=_keys("last_n_years"), targets=("persons", "publications"), topic=True),
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_condition(raw: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """One condition checked against the whitelist; the value itself is only ever passed as a query parameter."""
    if not isinstance(raw, dict):
        raise _Unsupported(f"condition is not an object: {raw!r}")
    name, op = raw.get("field"), raw.get("op")
    if name not in fields:
        raise _Unsupported(f"field {name!r} is not allowed here")
    if op not in OPS:
        raise _Unsupported(f"unknown operator {op!r}")
    if op in NULL_OPS:
        return {"field": name, "op": op}
    value = raw.get("value")
    if value is None:
        raise _Unsupported(f"condition on {name!r} has no value")
    if op == "in":
        if not isinstance(value, list) or not value:
            raise _Unsupported(f"'in' on {name!r} needs a non-empty list")
    elif isinstance(value, list):
        raise _Unsupported(f"a list is only allowed with 'in' (field {name!r})")
    return {"field": name, "op": op, "value": value}


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
    extra: int = 0

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
    fallback_reson: str | None = None

    @field_validator("template", mode="before")
    @classmethod
    def _normalise_template(cls, value: Any) -> str:
        # A number, a padded string or an unknown id from the model must not
        # sink the whole plan: whatever is not a template falls back to "none".
        text = str(value).strip().lower() if value is not None else "none"
        return text if text in TEMPLATE_IDS else "none"

    # def model_dump(self, **kwargs) -> dict[str, any]:
    #     return super().model_dump(**kwargs)

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
        self._validate_template()
        self._derive_type()
        self._apply_template()
        return self

    def _validate_template(self) -> None:
        """Keep the template only if the code can run it as written; otherwise "none"."""
        if self.template == "none":
            return
        try:
            self.params = self._check_template(TEMPLATE_SPECS[self.template])
        except _Unsupported as exc:
            self.downgraded_from = self.template
            self.fallback_reason = str(exc)
            self.template = "none"
            self.params = {}

    def _check_template(self, spec: TemplateSpec) -> dict[str, Any]:
        """The params cleaned and completed, or _Unsupported. Nothing is dropped silently."""
        template = self.template
        params = dict(self.params)

        unknown = set(params) - spec.keys
        if unknown:
            raise _Unsupported(f"parameters {sorted(unknown)} do not belong to template {template}")

        persons = [e for e in self.entities if e.kind == "person"]
        departments = [e for e in self.entities if e.kind == "department"]
        if len(persons) > spec.max_persons or len(departments) > spec.max_departments:
            raise _Unsupported(f"{len(persons)} person(s) and {len(departments)} department(s) do not fit "
                               f"template {template}")
        if spec.need_department and not departments:
            raise _Unsupported(f"template {template} needs a department")
        if spec.topic and not self.core:
            raise _Unsupported(f"template {template} needs topic phrases")

        # target: a template with one possible target does not need it written.
        target = params.get("target")
        if target is None and len(spec.targets) == 1:
            target = spec.targets[0]
        if target not in spec.targets:
            raise _Unsupported(f"target {target!r} does not fit template {template}")
        params["target"] = target

        # metric: required in templates 1-4, only "exists" is meaningful in 5-10 ("list" is the default there).
        metric = params.pop("metric", None)
        if spec.metrics:
            if metric not in spec.metrics:
                raise _Unsupported(f"metric {metric!r} does not fit template {template}")
            params["metric"] = metric
        elif metric == "exists":
            params["metric"] = "exists"
        elif metric not in (None, "list"):
            raise _Unsupported(f"metric {metric!r} does not fit template {template}")

        if "group_by" in params and params["group_by"] not in spec.group_by:
            raise _Unsupported(f"group_by {params['group_by']!r} does not fit template {template}")

        order, limit = params.get("order"), params.get("limit")
        if order is not None and order not in ORDERS:
            raise _Unsupported(f"unknown order {order!r}")
        if limit is not None and (not _is_int(limit) or limit <= 0):
            raise _Unsupported(f"limit {limit!r} is not a positive integer")
        if (limit is not None or "rank_by" in params) and order is None:
            params["order"] = "desc"  # "top", "most" and "who has the most" all rank from the top

        if "last_n_years" in params and (not _is_int(params["last_n_years"]) or params["last_n_years"] <= 0):
            raise _Unsupported(f"last_n_years {params['last_n_years']!r} is not a positive integer")


        if "no_dept" in params:
            if not isinstance(params["no_dept"], bool):
                raise _Unsupported("no_dept is not a boolean")
            if not params["no_dept"]:
                del params["no_dept"]
            elif departments:
                raise _Unsupported("no_dept contradicts the department named in the question")

        if "coauthor_is_itmo" in params and not isinstance(params["coauthor_is_itmo"], bool):
            raise _Unsupported("coauthor_is_itmo is not a boolean")
        if "rank_by" in params and params["rank_by"] not in RANK_BY:
            raise _Unsupported(f"unknown rank_by {params['rank_by']!r}")

        for key, fields in spec.conditions.items():
            if key not in params:
                continue
            raw = params[key]
            if not isinstance(raw, list):
                raise _Unsupported(f"{key} is not a list")
            cleaned = [_clean_condition(item, fields) for item in raw]
            if cleaned:
                params[key] = cleaned
            else:
                del params[key]

        if template == "2" and (params.get("metric") == "ratio") != ("ratio_filters" in params):
            raise _Unsupported("a ratio needs ratio_filters, and ratio_filters only go with a ratio")

        if template == "3" and self._derive_subtype("3", self.entity_slots()) is None:
            raise _Unsupported("this combination of people and departments has no variant of template 3")

        if template == "5":
            pattern = params.get("pattern")
            if pattern not in PATTERNS:
                raise _Unsupported(f"unknown pattern {pattern!r} of template 5")
            if (pattern == "author_with_repo") != ("filters" in params):
                raise _Unsupported("filters belong to the pattern author_with_repo, and that pattern needs them")

        if template == "9":
            # "Topic and code" is what the template is: the flag is implied, but it cannot be switched off.
            if params.get("require_code", True) is not True:
                raise _Unsupported("template 9 requires code")
            params["require_code"] = True
            if params.setdefault("link", "mentions") not in LINKS:
                raise _Unsupported(f"unknown link {params['link']!r}")

        return params

    def _derive_type(self) -> None:
        if self.template != "none":
            self.type = TEMPLATE_KIND[self.template]
        elif self.entities and self.core:
            # Without a template the type follows from what was found, whatever the model labelled it.
            self.type = "mixed"
        elif self.entities:
            self.type = "graph"
        else:
            self.type = "topic"
        if self.type == "topic":
            self.graph_type = "topic_subgraph"
        elif self.type == "graph" and self.graph_type == "topic_subgraph" and self.entities:
            self.graph_type = "person_profile" if self.entities[0].kind == "person" else "department_profile"

    def _apply_template(self) -> None:
        if self.template == "none":
            self.subtype = None
            return
        self.subtype = self._subtype()

        # What the answer contains follows from the parameters, not from the
        # model's guess: a list returns rows of the target, "is there" the rows
        # and the verdict, anything else a count.
        target = self.params["target"]
        metric = self.params.get("metric", "list")
        rows = [target]
        if self.template == "9" and target == "publications":
            rows = ["publications", "repositories"]  # the point of the question is the code next to the paper
        if metric == "list" and not self.params.get("group_by"):
            self.expected_values = rows
        elif metric == "exists":
            self.expected_values = [*rows, "counts"]
        else:
            self.expected_values = ["counts"]

        years = self.params.get("last_n_years")
        if _is_int(years) and years > 0 and self.filter.year_from is None and self.filter.year_to is None:
            self.filter.year_from = _current_year() - years + 1

    def _subtype(self) -> Subtype | None:
        if self.template == "3":
            return self._derive_subtype("3", self.entity_slots())
        if self.template == "5":
            return PATTERNS.get(self.params.get("pattern"))
        return None

    def entity_slots(self) -> EntitySlots:
        persons = [e for e in self.entities if e.kind == "person"]
        departments = [e for e in self.entities if e.kind == "department"]
        if persons:
            # A department next to a person is always the partner; any further one has no slot.
            return EntitySlots(persons, None, departments[0] if departments else None,
                               extra=max(len(departments) - 1, 0))
        subject = next((e for e in departments if e.role == "subject"), None)
        partner = next((e for e in departments if e.role == "partner"), None)
        free = [e for e in departments if e.role is None]
        if subject is None and free:
            subject = free.pop(0)
        if partner is None and free:
            partner = free.pop(0)
        placed = (subject is not None) + (partner is not None)
        return EntitySlots([], subject, partner, extra=len(departments) - placed)

    def wanted_fields(self, section: str) -> set[str] | None:
        allowed = set(FIELDS.get(section, ()))
        chosen = {name for name in self.fields if name in allowed}
        return chosen or None

    @staticmethod
    def _derive_subtype(template: str, slots: EntitySlots) -> str | None:
        if template != "3" or slots.extra:
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
