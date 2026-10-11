"""What the overview says about the state of the work.

Four pages answer four questions a person opens the panel with: is anything
running, is anyone running it, how much is waiting for an answer, and is the
graph healthy. Reading all four means four page loads, so they are collected
here and shown on the first one.

Mongo only: the counts come from the queue, the review store and the last
saved health snapshot, so the overview keeps working while the graph is
down.
"""

from __future__ import annotations

from pymongo.database import Database
from pymongo.errors import PyMongoError

from pauk.admin import health
from pauk.jobs import store
from pauk.jobs.models import FINAL, JobKind, JobState
from pauk.storage import review


def _last_run(db: Database) -> dict | None:
    """The last pipeline run that finished, whatever it finished as."""
    row = db[store.COLLECTION].find_one(
        {"kind": str(JobKind.PIPELINE), "state": {"$in": [str(name) for name in FINAL]}},
        sort=[("created_at", -1)])
    if row is None:
        return None
    return {"state": row.get("state"), "at": row.get("finished_at") or row.get("created_at"),
            "failed": row.get("state") == str(JobState.FAILED)}


def collect(db: Database) -> dict:
    """The overview's summary, or an empty one when Mongo is silent.

    Empty rather than an error: the page it sits on is readable without it,
    and a panel that refuses to open because a count failed is worse than
    one that opens without the count.
    """
    try:
        running = [job for job in store.running(db)]
        return {
            "running": [{"kind": str(job.kind), "id": job.id} for job in running],
            "workers": store.workers_present(db),
            "pressing": review.count(db, pressing=True, answered=False, skipped=False),
            "last_run": _last_run(db),
            "health": _health(db),
        }
    except PyMongoError:
        return {}


def _health(db: Database) -> dict | None:
    """The verdict of the last health run, with the day it was taken."""
    saved = health.latest(db)
    if not saved:
        return None
    counted = health.verdict(saved.get("stats", {}).get("checks", []))
    return {"at": saved.get("computed_at"), **counted}
