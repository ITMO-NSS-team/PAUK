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
