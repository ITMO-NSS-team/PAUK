"""Taking turns over something only one runner may touch at a time.

Publishing a group, deduplicating the graph and exporting a snapshot all
rewrite large parts of Neo4j. Two of them at once leave the graph correct
by luck and the audit feed nonsense: interleaved batches, overrides
reapplied against a state that has already moved.

The lock is taken by the pipeline functions themselves rather than by the
worker, and that is the point. The likeliest collision is not two workers —
there is one — but somebody running `pauk publish graph` in a terminal
while the panel schedules the same thing.

Not a unique partial index (`unique=True, partialFilterExpression=...`):
mongomock refuses to create one, which would leave the whole mechanism
untestable. One document per resource, keyed by `_id`, does the same job in
one atomic operation.
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

# How long a lock stays valid without being renewed. Long enough that a
# publish never loses its own lock mid-run, short enough that a machine
# killed halfway does not wedge the queue until somebody notices.
LEASE_MINUTES = 15


class Busy(RuntimeError):
    """Someone else holds the resource.

    Carries who and since when: "the graph is busy" sends a person looking
    for a process, while "held by admin-cli on host:1234 since 12:03" ends
    the search.
    """


def this_process() -> str:
    """A name for the current runner, for the message the next one reads."""
    return f"{socket.gethostname()}:{os.getpid()}"


def acquire(db: Database, resource: str, owner: str) -> bool:
    """Take the resource, or report that it is taken.

    One update does all three cases. The filter matches a lock that has
    expired, so a run whose machine died is taken over; it matches nothing
    when the resource is free, and the upsert inserts; and it matches
    nothing when the lock is live and unexpired, where the upsert collides
    on `_id` and says so. Checking first and inserting second would leave a
    gap in which two runners both find it free.
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
    moment = now()
    result = db[COLLECTION].update_one(
        {"_id": resource, "owner": owner},
        {"$set": {"expires_at": moment + timedelta(minutes=LEASE_MINUTES)}})
    return result.matched_count > 0


def release(db: Database, resource: str, owner: str) -> bool:
    """Give the resource back. Never removes a lock somebody else took over."""
    return db[COLLECTION].delete_one({"_id": resource, "owner": owner}).deleted_count > 0


def holder(db: Database, resource: str) -> dict | None:
    """Who holds the resource right now, or None if nobody does."""
    row = db[COLLECTION].find_one({"_id": resource})
    if row is None:
        return None
    if aware(row["expires_at"]) < now():
        # Expired but not yet taken over: the queue reads this as free.
        return None
    return row


@contextmanager
def held(db: Database, resource: str, owner: str | None = None) -> Iterator[str]:
    """Hold the resource for the length of a block.

    Raises:
        Busy: Somebody else has it. The caller decides whether that is an
            error to report or a reason to wait — a CLI says so and stops,
            a worker puts its job back in the queue.
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
