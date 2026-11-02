"""Shared rewrites for references whose canonical identity changed."""

from __future__ import annotations

from pymongo.database import Database


def repoint_person_references(db: Database, id_map: dict[str, str]) -> int:
    """Move person identity/provenance references to canonical owners.

    Both the ordinary prepared-data dedup and the one-time migration perform
    this exact rewrite. Keeping it here prevents one path from forgetting a
    compatibility ledger that the other path updates.
    """
    if not id_map:
        return 0
    for duplicate_id, canonical_id in id_map.items():
        db.identity_keys.update_many(
            {"entity": "person", "entity_id": duplicate_id},
            {"$set": {"entity_id": canonical_id}},
        )
    changed = 0
    duplicate_ids = list(id_map)
    for publication in db.publications.find({
        "$or": [
            {"source_records.authors.person_id": {"$in": duplicate_ids}},
            {"versions.authors.person_id": {"$in": duplicate_ids}},
        ]
    }):
        update = {}
        for field in ("source_records", "versions"):
            records = publication.get(field) or []
            field_changed = False
            for record in records:
                for author in record.get("authors") or []:
                    canonical_id = id_map.get(author.get("person_id"))
                    if canonical_id:
                        author["person_id"] = canonical_id
                        field_changed = True
            if field_changed:
                update[field] = records
        if update:
            changed += db.publications.update_one(
                {"_id": publication["_id"]},
                {"$set": update},
            ).modified_count
    return changed
