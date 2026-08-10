from __future__ import annotations

from pymongo import MongoClient

from pauk.settings import Settings


def get_mongo_client(config: Settings) -> MongoClient:
    """Open a MongoDB client for the raw/prepared intermediate storage.

    Callers own the returned client and must close() it when done, same as
    Neo4jClient (see pauk/graph/client.py).
    """
    return MongoClient(config.mongo_uri)
