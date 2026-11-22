from __future__ import annotations

from collections.abc import Iterable

from pauk.aliases import flatten_merges
from pauk.models import Person
from pauk.models.identity import (
    normalize_external_value,
    normalize_orcid_reference,
    verified_orcid,
)


def openalex_orcid_references(db, *, group: str | None = None) -> dict[str, str | None]:
    """Return the latest raw ORCID reference for every fetched OpenAlex author."""
    orcids: dict[str, str | None] = {}
    aliases = {}
    query = {"source": "openalex_authors"}
    if group is not None:
        query["group"] = group
    cursor = db.raw.find(query).sort("fetched_at", 1)
    for envelope in cursor:
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            continue
        author_id = normalize_external_value("openalex", payload["id"])
        value = payload.get("orcid")
        if not author_id or (value is not None and not isinstance(value, str)):
            continue
        orcids[author_id] = normalize_orcid_reference(value) or None
        aliases.pop(author_id, None)
        request = envelope.get("request")
        requested = request.get("author_id") if isinstance(request, dict) else None
        if isinstance(requested, str):
            key = normalize_external_value("openalex", requested)
            if key and key != author_id:
                aliases[key] = author_id
    for alias, canonical in flatten_merges(list(aliases.items())):
        orcids[alias] = orcids[canonical]
    return orcids


def person_orcid_references(people: Iterable[Person], raw_orcids: dict[str, str | None]) -> dict[str, str | None]:
    """Index source-owned references by internal person ID for deduplication."""
    return {
        person.id: raw_orcids.get(normalize_external_value("openalex", person.openalex_id or person.id),
                                  normalize_orcid_reference(person.orcid) or None)
        for person in people
    }


def trusted_openalex_orcids(db) -> dict[str, str | None]:
    """Return only valid source-owned ORCIDs from raw OpenAlex author rows."""
    return {
        author_id: verified_orcid(orcid)
        for author_id, orcid in openalex_orcid_references(db).items()
    }
