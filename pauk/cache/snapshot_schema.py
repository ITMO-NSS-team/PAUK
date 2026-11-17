"""Structural contract shared by snapshot export, reading and map builds."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

SNAPSHOT_COLUMNS: dict[str, tuple[str, ...]] = {
    "persons": (
        "id",
        "is_itmo",
        "openalex_id",
        "surname_ru",
        "first_name_ru",
        "second_name_ru",
        "surname_en",
        "first_name_en",
        "second_name_en",
        "name_ru",
        "name_en",
        "name_variants",
        "other_names",
        "degree",
        "github",
        "orcid",
        "google_scholar",
        "email",
        "affiliations",
        "created_at",
        "updated_at",
    ),
    "publications": (
        "id",
        "title",
        "type",
        "fields",
        "journal",
        "doi",
        "has_code",
        "code_url",
        "funding",
        "openalex_url",
        "abstract",
        "versions",
        "year",
        "publication_date",
        "created_at",
        "updated_at",
    ),
    "repositories": (
        "id",
        "name",
        "url",
        "description",
        "stars_num",
        "has_readme",
        "license",
        "contributors",
        "language",
        "topics",
        "archived",
        "forks_num",
        "is_fork",
        "owner",
        "owner_type",
        "access_date",
        "last_updated",
        "created_at",
        "updated_at",
    ),
    "departments": ("id", "name_ru", "name_en", "name_variants", "context_aliases", "kind"),
    "organizations": ("id", "name_ru", "name_en", "ror_id", "country", "type"),
    "authorship": ("pid", "per", "position", "is_corresponding"),
    "person_depts": ("per", "did"),
    "pub_depts": ("pid", "did"),
    "repo_pubs": ("rid", "pid"),
    "mentions_repos": ("pid", "rid", "is_relevant"),
    "mentions_candidates": ("pid", "url", "host", "is_relevant"),
    "repo_persons": ("rid", "per", "role"),
    "repo_depts": ("rid", "did"),
    "pub_versions": ("version_pid", "pid", "bucket", "mergeable", "status"),
}


def validate_snapshot(graph: Any, *, tables: Collection[str] | None = None) -> None:
    """Reject missing tables/columns before layout; null values and extra keys are allowed.

    ``tables`` limits validation to a partial export's fresh or retained tables.
    Every exported column must be present even when its property value is null.
    Empty tables are valid and need no placeholder rows.
    """

    def fail(problem: str) -> None:
        raise ValueError(f"invalid graph snapshot: {problem}; rebuild with 'pauk cache export'")

    if not isinstance(graph, dict):
        fail("expected a JSON object")
    names = set(SNAPSHOT_COLUMNS) if tables is None else set(tables)
    unknown = names - SNAPSHOT_COLUMNS.keys()
    if unknown:
        fail(f"unknown tables {sorted(unknown)}")
    missing = names - graph.keys()
    if missing:
        fail(f"missing tables {sorted(missing)}")
    for name in sorted(names):
        rows = graph[name]
        if not isinstance(rows, list):
            fail(f"table '{name}' must be a list")
        required = set(SNAPSHOT_COLUMNS[name])
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                fail(f"table '{name}', row {index + 1} must be an object")
            missing = required - row.keys()
            if missing:
                fail(f"table '{name}', row {index + 1} missing columns {sorted(missing)}")
