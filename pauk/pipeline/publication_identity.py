"""Confirmed work identities for person-resolution evidence, without graph writes."""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pauk.aliases import flatten_merges


def publication_identity_map(
    work_ids: Iterable[str], details: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    """Group valid equal DOIs and validated historical aliases, never titles."""
    details = details or {}
    aliases = [
        (alias, work_id)
        for work_id, row in details.items()
        if isinstance(row.get("merged_ids"), (list, tuple))
        for alias in row["merged_ids"]
        if isinstance(alias, str) and alias.strip()
    ]
    confirmed_aliases = flatten_merges(aliases, label="Publication")
    ids = set(work_ids) | set(details)
    ids.update(alias for alias, _ in confirmed_aliases)
    parents = {work_id: work_id for work_id in ids}

    def find(work_id: str) -> str:
        while parents[work_id] != work_id:
            parents[work_id] = parents[parents[work_id]]
            work_id = parents[work_id]
        return work_id

    def join(first: str, second: str) -> None:
        first, second = sorted((find(first), find(second)))
        parents[second] = first

    for alias, survivor in confirmed_aliases:
        join(alias, survivor)
    doi_owner: dict[str, str] = {}
    for work_id, row in details.items():
        doi = row.get("doi")
        if not isinstance(doi, str):
            continue
        doi = doi.strip().lower()
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
            doi = doi.removeprefix(prefix).strip()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            continue
        join(work_id, doi_owner.setdefault(doi, work_id))
    return {work_id: find(work_id) for work_id in ids}
