from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel
from pymongo.database import Database

M = TypeVar("M", bound=BaseModel)


class PreparedStore:
    # Collection name per prepared entity - 1:1 with the old FILES map.
    COLLECTIONS = {
        "publications": "publications",
        "persons": "persons",
        "departments": "departments",
        "organizations": "organizations",
        "repositories": "repositories",
        "github_profiles": "github_profiles",
        "repo_links": "repo_links",
    }

    # Field that identifies a row within its collection and becomes the
    # document's _id. Every entity but repo_links has a real "id" field;
    # RepoLink has none - it's one row per publication, keyed by
    # publication_id.
    KEY_FIELDS = {"repo_links": "publication_id"}

    def __init__(self, db: Database, group: str) -> None:
        self.db = db
        self.group = group

    def _collection(self, entity: str):
        try:
            name = self.COLLECTIONS[entity]
        except KeyError as exc:
            raise ValueError(f"unknown prepared entity: {entity}") from exc
        return self.db[name]

    def _key_field(self, entity: str) -> str:
        return self.KEY_FIELDS.get(entity, "id")

    @classmethod
    def known_groups(cls, db: Database) -> list[str]:
        """Every group that has prepared rows, oldest name first.

        Rows carry the groups that claim them rather than belonging to one,
        so there is no collection of groups to read — the names are gathered
        from the rows themselves. Used by the panel, which must offer a
        group that exists instead of a box to mistype one into.
        """
        found: set[str] = set()
        for name in cls.COLLECTIONS.values():
            found.update(db[name].distinct("groups"))
        return sorted(found)

    def read_rows(self, entity: str) -> Iterator[dict]:
        cursor = self._collection(entity).find(
            {"groups": self.group},
            {"_id": False, "groups": False, "_version": False},
        )
        yield from cursor

    def read_models(self, entity: str, model: type[M]) -> Iterator[M]:
        for row in self.read_rows(entity):
            yield model.model_validate(row)

    def get_rows(self, entity: str, ids: Iterable[str]) -> Iterator[dict]:
        """Look up specific rows by their key field, across every group.

        Unlike read_rows, this isn't scoped to self.group - it's for a
        stage that needs the current global state of a row it already
        knows the id of (see OpenAlexNormalizer), not "everything my group
        has touched".
        """
        ids = [i for i in ids if i]
        if not ids:
            return
        key_field = self._key_field(entity)
        cursor = self._collection(entity).find(
            {key_field: {"$in": ids}},
            {"_id": False, "groups": False, "_version": False},
        )
        yield from cursor

    def get_models(self, entity: str, ids: Iterable[str], model: type[M]) -> Iterator[M]:
        for row in self.get_rows(entity, ids):
            yield model.model_validate(row)

    def write_rows(self, entity: str, rows: Iterable[dict]) -> None:
        """Set this group's complete state for `entity` to exactly `rows`.

        Every stage reads its group's full working set for an entity,
        mutates it, and writes the whole thing back - the same contract the
        old whole-file rewrite had. A row this group held before but didn't
        re-include here (folded into another row by dedup, renamed by
        normalize) has this group's claim retracted; a document no group
        claims any more is deleted so it doesn't linger unreachable.
        """
        key_field = self._key_field(entity)
        collection = self._collection(entity)
        revisions = self.db.revisions
        written_ids = []
        for row in rows:
            row_id = row[key_field]
            written_ids.append(row_id)
            self._upsert_row(entity, row, collection, revisions)
        collection.update_many(
            {"groups": self.group, "_id": {"$nin": written_ids}},
            {"$pull": {"groups": self.group}},
        )
        collection.delete_many({"groups": {"$size": 0}})

    def write_models(self, entity: str, rows: Iterable[BaseModel]) -> None:
        # mode="json" so date/datetime fields serialize to strings - pymongo's
        # BSON encoder rejects a bare datetime.date (only datetime.datetime).
        # exclude_none=False (the default) so a field cleared to None reaches
        # write_rows and is $unset - dropping it here would leave the old
        # value in Mongo forever, since $set only ever touches keys present
        # in its payload.
        self.write_rows(entity, (row.model_dump(mode="json", by_alias=True) for row in rows))

    def upsert_models(self, entity: str, rows: Iterable[BaseModel]) -> None:
        """Persist changed rows without redefining this group's full membership.

        Enrichment stages use this after an external request completes.  Unlike
        write_models(), it never removes the group marker from untouched rows.
        """
        collection = self._collection(entity)
        revisions = self.db.revisions
        for model in rows:
            self._upsert_row(entity, model.model_dump(mode="json", by_alias=True), collection, revisions)

    def _upsert_row(self, entity: str, row: dict, collection, revisions) -> None:
        key_field = self._key_field(entity)
        row_id = row[key_field]
        before = collection.find_one({"_id": row_id})
        before_content = {k: v for k, v in (before or {}).items() if k not in ("_id", "groups", "_version")}
        row_content = {k: v for k, v in row.items() if v is not None}
        content_changed = before is None or before_content != row_content
        if not content_changed and before is not None and "_version" in before:
            collection.update_one({"_id": row_id}, {"$addToSet": {"groups": self.group}})
            return
        new_version = (before or {}).get("_version", 0) + 1
        update: dict = {
            "$set": {**row_content, "_version": new_version},
            "$addToSet": {"groups": self.group},
        }
        unset_fields = {k for k, v in row.items() if v is None}
        if unset_fields:
            update["$unset"] = dict.fromkeys(unset_fields, "")
        collection.update_one({"_id": row_id}, update, upsert=True)
        if content_changed and before is not None:
            revisions.insert_one(
                {
                    "entity_type": entity,
                    "entity_id": row_id,
                    "version": before.get("_version", 0),
                    "snapshot": before,
                    "replaced_by_group": self.group,
                    "replaced_at": datetime.now(UTC).isoformat(),
                }
            )
