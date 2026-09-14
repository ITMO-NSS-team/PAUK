from __future__ import annotations

import logging

from pymongo import MongoClient
from pymongo.database import Database

from pauk.settings import Settings

logger = logging.getLogger(__name__)


def get_mongo_client(config: Settings, timeout_ms: int | None = None) -> MongoClient:
    """Open a MongoDB client for the raw/prepared intermediate storage.

    Callers own the returned client and must close() it when done, same as
    Neo4jClient (see pauk/graph/client.py).

    Args:
        timeout_ms: How long to wait for a reachable server before giving
            up. The driver's own default is thirty seconds, which suits a
            command that would rather wait than fail; a caller answering a
            web request passes something short.
    """
    if timeout_ms is None:
        return MongoClient(config.mongo_uri)
    return MongoClient(config.mongo_uri, serverSelectionTimeoutMS=timeout_ms)


#: Collections created with stronger compression than the server default.
#: `raw` keeps a verbatim copy of every API answer and is three quarters of
#: the database; `revisions` keeps a full snapshot per changed row. Both are
#: repetitive JSON, which zstd folds several times over, and neither is read
#: often enough for the extra work to show.
COMPRESSED = ("raw", "revisions")


def ensure_compression(db: Database) -> list[str]:
    """Create the heavy collections compressed, before anything writes to them.

    Only ones that do not exist yet. WiredTiger takes the compressor when
    the collection is created, and changing it afterwards applies to new
    blocks alone — an existing collection needs `collMod` and a rewrite,
    which is an operator's decision with a lock attached, not something a
    command does on startup.

    Returns:
        The collections it created, empty on a database that already has
        them or on a server that does not take the option.
    """
    created = []
    try:
        existing = set(db.list_collection_names())
        for name in COMPRESSED:
            if name in existing:
                continue
            db.create_collection(
                name, storageEngine={"wiredTiger": {"configString": "block_compressor=zstd"}})
            created.append(name)
    except Exception as error:
        # A double that does not implement collection options, an old
        # server, an account without the right. None of that is a reason to
        # refuse to run: the collection is made by the first write anyway,
        # just with the default compressor.
        logger.info("collections left at the default compression: %s", error)
    return created


def ensure_indexes(db: Database) -> None:
    """Create indexes the storage layer relies on. Idempotent - safe to call
    on every command startup, same spot as Neo4j's create_constraints()."""
    ensure_compression(db)
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
    # A worker asks for the oldest queued job on every turn of its loop, and
    # the panel warns an editor whenever a run is under way.
    db.jobs.create_index([("state", 1), ("created_at", 1)])
    db.jobs.create_index([("created_at", -1)])
    # The review queue is opened on the unanswered questions, oldest first,
    # and every dedup run reads back every answer given so far.
    db.review_pairs.create_index([("verdict", 1), ("seen_at", 1)])
    db.review_pairs.create_index([("members", 1)])
