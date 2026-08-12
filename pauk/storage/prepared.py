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
            before = collection.find_one({"_id": row_id})
            before_content = {k: v for k, v in (before or {}).items() if k not in ("_id", "groups", "_version")}
            # A None value means "clear this field" - stored documents never
            # carry an explicit null (cleared fields are $unset, not $set to
            # None), so both sides must drop them to compare like with like.
            row_content = {k: v for k, v in row.items() if v is not None}
            content_changed = before is None or before_content != row_content
            # A document written before _version existed has no such field -
            # even with unchanged content, it still needs one backfilled to
            # keep the invariant "every stored document carries _version".
            if not content_changed and before is not None and "_version" in before:
                collection.update_one({"_id": row_id}, {"$addToSet": {"groups": self.group}})
                continue
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
