"""Apply durable version intents through the current-policy reconciler."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from neo4j.exceptions import DriverError, Neo4jError
from pymongo.errors import PyMongoError

from pauk.jobs.locks import held
from pauk.jobs.models import GRAPH
from pauk.settings import Settings
from pauk.storage import version_repair

from .audit import actor_context, audited_client
from .client import resolve_folded_id
from .mutations import MutationError, NotFound
from .overrides import COLLECTION, VERSION_RELATIONSHIP, relationship_override_id
from .version_link import reconcile_version_pair

logger = logging.getLogger(__name__)


@contextmanager
def version_mutation_lock(db, triple):
    """Serialize manual version writers with publish/reconciliation, including --once."""
    if tuple(triple) != VERSION_RELATIONSHIP:
        yield
        return
    if db is None:
        raise ValueError("version relationship writes require MongoDB for GRAPH locking")
    with held(db, GRAPH):
        yield


def apply_pending_version_repair_locked(
    client, db, collection_name: str, document_id: str, token: str, *, defer_on_error: bool = True
) -> tuple[str, dict[str, int]]:
    """Apply current authority under GRAPH, never replaying a stored verdict.

    Set defer_on_error=False when the caller records failed attempts itself.
    """
    intent = version_repair.load(db, collection_name, document_id, token)
    if intent is None:
        return "version_obsolete", {"version_repairs_obsolete": 1}
    source = db[collection_name].find_one({"_id": document_id, f"{version_repair.FIELD}.token": token})
    if source is None:
        return "version_obsolete", {"version_repairs_obsolete": 1}
    members = intent["members"]
    previous = None
    if collection_name == "graph_overrides" and "snapshot" in source:
        # members are unordered, but a deletion snapshot belongs to the original direction.
        direction = tuple(resolve_folded_id(client, "Publication", source[key]) for key in ("src_id", "target_id"))
        if direction[0] != direction[1]:
            previous = {direction: source["snapshot"]}
    try:
        done, written, removed = reconcile_version_pair(
            client,
            db,
            members,
            reopen=intent["mode"] == "reopen",
            previous=previous,
        )
        if done == "version_missing" and source.get("active") and source.get("op") == "link":
            raise NotFound("a manually claimed version endpoint is missing")
        acknowledged = version_repair.ack(db, collection_name, document_id, token)
    except Exception as error:
        if defer_on_error:
            # Keeping the obligation is more important than recording a retry time
            # during an outage of the very database that owns it.
            try:
                version_repair.defer(db, collection_name, document_id, token, error=f"{type(error).__name__}: {error}")
            except PyMongoError:
                logger.warning("could not postpone version repair %s/%s", collection_name, document_id)
        raise
    return (done if acknowledged else "version_waiting"), {
        "version_edges": written,
        "version_edges_removed": removed,
        "version_repairs": int(acknowledged),
        "version_repairs_pending": int(not acknowledged),
    }


def try_apply_version_override_locked(client, db, src_id: str, tgt_id: str) -> bool:
    """Return whether a saved manual change still needs worker projection."""
    document_id = relationship_override_id(*VERSION_RELATIONSHIP, src_id, tgt_id)
    row = db[COLLECTION].find_one({"_id": document_id}) or {}
    intent = row.get(version_repair.FIELD)
    if not intent:
        return False
    try:
        _, result = apply_pending_version_repair_locked(client, db, COLLECTION, document_id, intent["token"])
    except (MutationError, Neo4jError, DriverError, PyMongoError):
        logger.warning("manual version change %s is saved and pending", document_id)
        return True
    return bool(result.get("version_repairs_pending") or result.get("version_repairs_obsolete"))


def run_pending_version_repair(
    config: Settings, db, collection_name: str, document_id: str, token: str, *, defer_on_error: bool = True
) -> dict[str, int]:
    """Apply a durable intent; a worker can take ownership of failure accounting."""
    with held(db, GRAPH), actor_context("etl-pipeline", source="versions-repair"):
        if version_repair.load(db, collection_name, document_id, token) is None:
            return {"version_repairs_obsolete": 1}
        client = audited_client(config, db)
        try:
            return apply_pending_version_repair_locked(
                client, db, collection_name, document_id, token, defer_on_error=defer_on_error,
            )[1]
        finally:
            client.close()
