from __future__ import annotations

from pymongo import MongoClient
from pymongo.database import Database

from pauk.settings import Settings


def get_mongo_client(config: Settings) -> MongoClient:
    """Open a MongoDB client for the raw/prepared intermediate storage.

    Callers own the returned client and must close() it when done, same as
    Neo4jClient (see pauk/graph/client.py).
    """
    return MongoClient(config.mongo_uri)


def ensure_indexes(db: Database) -> None:
    """Create indexes the storage layer relies on. Idempotent - safe to call
    on every command startup, same spot as Neo4j's create_constraints()."""
    db.revisions.create_index([("entity_type", 1), ("entity_id", 1), ("version", 1)])
    db.raw.create_index([("source", 1), ("group", 1), ("fetched_at", 1)])
    db.raw.create_index([("source", 1), ("fetched_at", 1)])
    # The panel's change feed reads the audit two ways: the history of one
    # entity, and everything one person did.
    db.audit.create_index([("entity_type", 1), ("entity_id", 1), ("timestamp", -1)])
    db.audit.create_index([("actor", 1), ("timestamp", -1)])
    # The unfiltered feed — the page the panel opens on — sorts by time
    # alone. Without this it is a collection scan plus an in-memory sort,
    # over a collection nothing ever trims.
    db.audit.create_index([("timestamp", -1)])
    # Reapplied after every publish and every graph dedup, so the lookup of
    # what is currently in force has to be cheap.
    db.graph_overrides.create_index([("active", 1), ("label", 1), ("op", 1)])
