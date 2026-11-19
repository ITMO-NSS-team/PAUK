from __future__ import annotations

from pauk.models.identity import (
    normalize_external_value,
    normalize_orcid_reference,
    verified_orcid,
)


def openalex_orcid_references(db) -> dict[str, str | None]:
    """Return the latest raw ORCID reference for every fetched OpenAlex author."""
    orcids: dict[str, str | None] = {}
    cursor = db.raw.find({"source": "openalex_authors"}).sort("fetched_at", 1)
    for envelope in cursor:
        payload = envelope.get("payload") or {}
        author_id = normalize_external_value("openalex", str(payload.get("id") or ""))
        if author_id:
            orcids[author_id] = normalize_orcid_reference(payload.get("orcid")) or None
    return orcids


def trusted_openalex_orcids(db) -> dict[str, str | None]:
    """Return only valid source-owned ORCIDs from raw OpenAlex author rows."""
    return {
        author_id: verified_orcid(orcid)
        for author_id, orcid in openalex_orcid_references(db).items()
    }
