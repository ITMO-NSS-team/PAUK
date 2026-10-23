"""Rename the Russian-only fields of a graph built before university profiles.

The name fields became language-neutral when a second university arrived:
`name_ru` and the person name parts became `*_local`, and `is_itmo` became
`is_member`. The code reads only the new names, so an existing database must
be migrated once, or names silently disappear from the panel and the map and
every person reads as external.

Mongo: the prepared rows (persons, departments, organizations), the copies of
them that other features restore from (`revisions` snapshots,
`person_merge_sources`, the manual decisions in `graph_overrides`). The audit
journal is history and keeps the names it was written with.

Neo4j: the same properties on :Person, :Department and :Organization.

A field already present under its new name is left as it is and the old one
is dropped, so a rerun, or a run over half-migrated data, is safe.

Dry run by default. Take `snapshot_mongo.py` first.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import LiteralString, cast

from pymongo.database import Database

from pauk.graph.client import Neo4jClient
from pauk.settings import settings
from pauk.storage.mongo import get_mongo_client

RENAMES = {
    "name_ru": "name_local",
    "surname_ru": "surname_local",
    "first_name_ru": "first_name_local",
    "second_name_ru": "second_name_local",
    "is_itmo": "is_member",
}

# Prepared collection -> the fields it can carry.
PREPARED = {
    "persons": tuple(RENAMES),
    "departments": ("name_ru",),
    "organizations": ("name_ru",),
}

GRAPH_LABELS = {"Person": tuple(RENAMES), "Department": ("name_ru",), "Organization": ("name_ru",)}


def mongo_targets() -> list[tuple[str, dict, str, tuple[str, ...]]]:
    """(collection, filter, path prefix, fields) for every place an old name can sit."""
    targets = [(collection, {}, "", fields) for collection, fields in PREPARED.items()]
    targets += [
        ("revisions", {"entity_type": collection}, "snapshot.", fields)
        for collection, fields in PREPARED.items()
    ]
    targets.append(("person_merge_sources", {}, "snapshot.", PREPARED["persons"]))
    labels = {"Person": "persons", "Department": "departments", "Organization": "organizations"}
    for label, collection in labels.items():
        for prefix in ("fields.", "auto_value.", "snapshot."):
            targets.append(("graph_overrides", {"label": label}, prefix, PREPARED[collection]))
    return targets


def migrate_mongo(db: Database, apply: bool) -> dict[str, int]:
    """Documents holding each old path; renamed in place when apply is set."""
    counts: dict[str, int] = {}
    for collection, base, prefix, fields in mongo_targets():
        for old in fields:
            old_path, new_path = f"{prefix}{old}", f"{prefix}{RENAMES[old]}"
            query = {**base, old_path: {"$exists": True}}
            found = db[collection].count_documents(query)
            if not found:
                continue
            counts[f"{collection}:{old_path}"] = found
            if apply:
                _rename(db, collection, query, old_path, new_path)
    return counts


def _rename(db: Database, collection: str, query: dict, old_path: str, new_path: str) -> None:
    """Move one field per document.

    Written out rather than as $rename, which would overwrite a value already
    under the new name with the stale one. One write per document: this runs
    once, and the bulk API is what the test double cannot follow.
    """
    for document in db[collection].find(query):
        update: dict = {"$unset": {old_path: ""}}
        if _get(document, new_path) is _MISSING:
            update["$set"] = {new_path: _get(document, old_path)}
        db[collection].update_one({"_id": document["_id"]}, update)


_MISSING = object()


def _get(document: dict, path: str):
    value = document
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def migrate_graph(client: Neo4jClient, apply: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    with client.driver.session() as session:
        for label, fields in GRAPH_LABELS.items():
            for old in fields:
                new = RENAMES[old]
                match = f"MATCH (n:{label}) WHERE n.{old} IS NOT NULL"
                found = session.run(cast(LiteralString, f"{match} RETURN count(n) AS n")).single()["n"]
                if not found:
                    continue
                counts[f"{label}.{old}"] = found
                if apply:
                    session.run(cast(LiteralString, f"{match} SET n.{new} = coalesce(n.{new}, n.{old}) REMOVE n.{old}"))
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="rename (default: dry run)")
    parser.add_argument("--skip-graph", action="store_true", help="Mongo only")
    args = parser.parse_args()
    # Every count over a property the database never had draws a warning.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

    print(f"institution: {settings.institution.key}, mongo db: {settings.mongo_db}, neo4j: {settings.neo4j_uri}")
    client = get_mongo_client(settings)
    try:
        for path, count in migrate_mongo(client[settings.mongo_db], args.apply).items():
            print(f"  mongo {path}: {count}")
    finally:
        client.close()
    if not args.skip_graph:
        graph = Neo4jClient(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        try:
            for path, count in migrate_graph(graph, args.apply).items():
                print(f"  neo4j {path}: {count}")
        finally:
            graph.close()
    if not args.apply:
        print("\ndry run: nothing renamed; pass --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
