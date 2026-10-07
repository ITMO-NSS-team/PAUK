"""Index profiles: which node fields make up the text that is embedded.

A profile is the unit of an ablation study. Two profiles that differ in one
field (say, `readme`) are built side by side in Neo4j under their own
property and index names, the same questions are asked of both, and the
metrics tell whether the field helps. Field order matters only a little:
the model sees the first 512 tokens, so the most telling fields go first.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .readme import clean_readme

LABELS = ("Publication", "Repository")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")

# A README head of this length already holds what the project is about;
# the model would cut the rest at 512 tokens anyway.
README_CHARS = 2000
# Full texts are long; only their beginning fits the model, so this is a
# "first page" field, not retrieval over the whole paper.
FULL_TEXT_CHARS = 2000
# Fewer words than this after cleaning is a title or a badge line - no topic.
README_MIN_WORDS = 20


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _words(text: str) -> int:
    return len(re.findall(r"\w{3,}", text))


def _readme(row: dict[str, Any]) -> str:
    text = clean_readme(row.get("readme") or "")[:README_CHARS]
    return text if _words(text) >= README_MIN_WORDS else ""


# One function per field: what a node contributes to the text under that name.
FIELDS: dict[str, dict[str, Callable[[dict[str, Any]], str]]] = {
    "Publication": {
        "title": lambda row: _text(row.get("title")),
        "abstract": lambda row: _text(row.get("abstract")),
        # OpenAlex domains ("Computer Science"): the only topic hint a paper
        # without an abstract has, but coarse.
        "fields": lambda row: ", ".join(row.get("fields") or []),
        "journal": lambda row: _text(row.get("journal")),
        "full_text": lambda row: _text(row.get("full_text"))[:FULL_TEXT_CHARS],
    },
    "Repository": {
        "name": lambda row: _text(row.get("name")),
        "description": lambda row: _text(row.get("description")),
        "readme": _readme,
        # The README as GitHub serves it: badges, install commands and code
        # included - to measure what the cleaning is worth.
        "readme_raw": lambda row: _text(row.get("readme"))[:README_CHARS],
        # Titles of the papers the code implements: the only text a
        # repository without description or README has.
        "implemented": lambda row: "; ".join(row.get("implemented") or []),
    },
}


@dataclass(frozen=True)
class Profile:
    name: str
    publication: tuple[str, ...] = ()
    repository: tuple[str, ...] = ()
    # Front matter, peer reviews and stand-alone figures are not research
    # texts; a caption like "Graphical Abstract" wins any vague query.
    skip_non_research: bool = True
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"profile name {self.name!r}: lowercase letters, digits and _, starting with a letter")
        for label, names in (("Publication", self.publication), ("Repository", self.repository)):
            unknown = [n for n in names if n not in FIELDS[label]]
            if unknown:
                raise ValueError(f"unknown {label} fields {unknown}; known: {sorted(FIELDS[label])}")
        if not self.publication and not self.repository:
            raise ValueError("a profile needs fields for at least one label")

    def fields_for(self, label: str) -> tuple[str, ...]:
        return self.publication if label == "Publication" else self.repository

    def labels(self) -> tuple[str, ...]:
        return tuple(label for label in LABELS if self.fields_for(label))

    # Neo4j names. Property and index names carry the profile, so profiles
    # live side by side and dropping one leaves the others untouched.
    @property
    def text_property(self) -> str:
        return f"rag_text_{self.name}"

    @property
    def vector_property(self) -> str:
        return f"rag_vec_{self.name}"

    @property
    def hash_property(self) -> str:
        return f"rag_hash_{self.name}"

    def vector_index(self, label: str) -> str:
        return f"rag_vec_{self.name}_{label.lower()}"

    def text_index(self, label: str) -> str:
        return f"rag_text_{self.name}_{label.lower()}"

    def compose(self, label: str, row: dict[str, Any]) -> str:
        parts = [FIELDS[label][name](row) for name in self.fields_for(label)]
        return ". ".join(part.rstrip(". ") for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "publication": list(self.publication), "repository": list(self.repository),
                "skip_non_research": self.skip_non_research, "description": self.description}


# Starting points for the ablation: `full` is the reasoned choice, every
# other preset changes exactly one thing in it.
PRESETS: dict[str, Profile] = {
    profile.name: profile for profile in (
        Profile("full", ("title", "abstract"), ("name", "description", "readme", "implemented"),
                description="title + abstract; repository name, description, cleaned README head, implemented papers"),
        Profile("no_readme", ("title", "abstract"), ("name", "description", "implemented"),
                description="full without the README"),
        Profile("raw_readme", ("title", "abstract"), ("name", "description", "readme_raw", "implemented"),
                description="full with the README uncleaned"),
        Profile("no_implemented", ("title", "abstract"), ("name", "description", "readme"),
                description="full without the titles of implemented papers"),
        Profile("title_only", ("title",), ("name",),
                description="names only: the floor every other field is measured against"),
        Profile("with_fields", ("title", "abstract", "fields"), ("name", "description", "readme", "implemented"),
                description="full plus the OpenAlex domains of a paper"),
        Profile("with_full_text", ("title", "abstract", "full_text"), ("name", "description", "readme", "implemented"),
                description="full plus the first page of the full text, where there is one"),
    )
}


def resolve(name: str, publication: list[str] | None = None, repository: list[str] | None = None) -> Profile:
    """A preset by name, or an ad-hoc profile when fields are given."""
    if publication is None and repository is None:
        if name not in PRESETS:
            raise ValueError(f"no preset {name!r}; presets: {sorted(PRESETS)} (or pass the fields)")
        return PRESETS[name]
    base = PRESETS.get(name)
    return Profile(name, tuple(publication if publication is not None else (base.publication if base else ())),
                   tuple(repository if repository is not None else (base.repository if base else ())),
                   description="ad hoc")
