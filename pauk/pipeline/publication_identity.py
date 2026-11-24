"""Confirmed work identities for person-resolution evidence, without graph writes."""

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from pauk.aliases import flatten_merges
from pauk.models import PublicationVersion


def normalize_publication_doi(doi: object) -> str | None:
    """Use the same resolver-prefix and suffix handling in both dedup passes."""
    if not isinstance(doi, str):
        return None
    value = " ".join(doi.split()).casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        value = value.removeprefix(prefix).strip()
    return value.rstrip("/") or None


def publication_details_with_versions(
    details: Mapping[str, Mapping[str, Any]],
    versions: Mapping[str, list[PublicationVersion]],
) -> dict[str, dict[str, Any]]:
    """Expand confirmed source records identically for prepared and graph passes.

    A live source overrides copies in other ledgers. Conflicting historical
    candidates cannot contribute an arbitrary DOI or mixed source provenance.
    The caller's stored records are never modified.
    """
    result = {work_id: dict(row) for work_id, row in details.items()}
    candidates_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for work_id, row in details.items():
        aliases = row.get("merged_ids")
        scope = {work_id}
        if isinstance(aliases, (list, tuple)):
            scope.update(alias for alias in aliases if isinstance(alias, str) and alias.strip())
        ledger = versions.get(work_id, [])
        if aliases or ledger:
            result[work_id]["source_person_ids"] = []
        for version in ledger:
            source_id = version.openalex_id
            if source_id not in scope or (source_id in details and source_id != work_id):
                continue
            candidates_by_id[source_id].append({
                "doi": version.doi, "title": version.title, "year": version.year,
                "merged_ids": [],
                "source_person_ids": sorted({author.person_id for author in version.authors}),
            })
    for source_id, candidates in candidates_by_id.items():
        identities = {(row["year"], tuple(row["source_person_ids"])) for row in candidates}
        confirmed_dois = {
            doi for row in candidates
            if (doi := normalize_publication_doi(row["doi"])) and re.fullmatch(r"10\.\d{4,9}/\S+", doi)
        }
        if len(identities) > 1 or len(confirmed_dois) > 1:
            source = result.setdefault(source_id, {"doi": None, "title": None, "merged_ids": []})
            source.update(year=None, source_person_ids=[], source_record_conflict=True)
        else:
            candidate = min(candidates, key=lambda row: (
                normalize_publication_doi(row["doi"]) not in confirmed_dois,
                json.dumps(row, sort_keys=True),
            ))
            if source_id in details:
                result[source_id].update(
                    year=candidate["year"], source_person_ids=candidate["source_person_ids"],
                )
            else:
                result[source_id] = candidate
    return result


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
        doi = normalize_publication_doi(row.get("doi"))
        if doi is None:
            continue
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            continue
        join(work_id, doi_owner.setdefault(doi, work_id))
    return {work_id: find(work_id) for work_id in ids}
