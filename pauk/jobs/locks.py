"""Taking turns over something only one runner may touch at a time.

Two publishes at once leave interleaved batches and an audit feed
describing an order that never happened.

The lock is taken by the pipeline functions, not by the worker: the
likeliest collision is somebody running `pauk publish graph` in a terminal
while the panel schedules the same thing.

Not a unique partial index, because mongomock refuses to create one.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from pauk.jobs.models import aware, now

logger = logging.getLogger(__name__)

COLLECTION = "job_locks"

# Long enough that a publish never loses its own lock mid-run, short enough
# that a machine killed halfway does not wedge the queue until somebody
# notices.
LEASE_MINUTES = 15


class Busy(RuntimeError):
    """Someone else holds the resource. Carries who and since when."""


def this_process() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def acquire(db: Database, resource: str, owner: str) -> bool:
    """Take the resource, or report that it is taken.

    One update covers all three cases. An expired lock matches the filter
    and is taken over; a free resource matches nothing and the upsert
    inserts; a live lock matches nothing and the upsert collides on `_id`.
    Checking first and inserting second would leave a gap.
    """
    moment = now()
    try:
        db[COLLECTION].update_one(
            {"_id": resource, "expires_at": {"$lt": moment}},
            {"$set": {"owner": owner, "acquired_at": moment,
                      "expires_at": moment + timedelta(minutes=LEASE_MINUTES)}},
            upsert=True)
    except DuplicateKeyError:
        return False
    return True


def renew(db: Database, resource: str, owner: str) -> bool:
    """Push the lease out. Only the holder can, and only while it holds."""
    result = db[COLLECTION].update_one(
        {"_id": resource, "owner": owner},
        {"$set": {"expires_at": now() + timedelta(minutes=LEASE_MINUTES)}})
    return result.matched_count > 0


def release(db: Database, resource: str, owner: str) -> bool:
    """Give the resource back, never a lock somebody else took over."""
    return db[COLLECTION].delete_one({"_id": resource, "owner": owner}).deleted_count > 0


def holder(db: Database, resource: str) -> dict | None:
    """Who holds the resource, or None. An expired lock reads as free."""
    row = db[COLLECTION].find_one({"_id": resource})
    if row is None or aware(row["expires_at"]) < now():
        return None
    return row


@contextmanager
def held(db: Database, resource: str, owner: str | None = None) -> Iterator[str]:
    """Hold the resource for the length of a block.

    Raises:
        Busy: Somebody else has it. The caller decides what that means: a
            CLI says so and stops, a worker puts its job back in the queue.
    """
    owner = owner or this_process()
    if not acquire(db, resource, owner):
        current = holder(db, resource) or {}
        raise Busy(
            f"{resource} is busy: held by {current.get('owner', 'someone')} "
            f"since {current.get('acquired_at', 'a moment ago')}")
    logger.info("holding %s as %s", resource, owner)
    try:
        yield owner
    finally:
        release(db, resource, owner)
        logger.info("released %s", resource)
