"""Where a long CLI command is, kept in MongoDB for an outside watcher.

The progress bars stay silent when stderr is not a terminal, so a run started
by a script leaves no trace of which stage it is in or how far it got.
`scripts/watch_institutions.py` reads this record instead.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

COLLECTION = "cli_progress"
# Enricher reports a stage's own progress as "<label> <done>/<total>".
_COUNTED = re.compile(r"(?P<label>.*) (?P<done>\d+)/(?P<total>\d+)")


class ProgressRecord:
    """An `OnStage` callback that keeps one document per group up to date."""

    def __init__(self, db: Any, command: str, group: str | None) -> None:
        self.rows = db[COLLECTION]
        self.key = {"_id": group or command}
        self.command = command

    def __enter__(self) -> ProgressRecord:
        now = datetime.now(UTC)
        self._set({"command": self.command, "pid": os.getpid(), "started_at": now, "updated_at": now,
                   "finished_at": None, "outcome": None, "stage": None, "stage_started_at": None,
                   "stage_index": None, "stage_total": None, "label": None, "rows_done": None,
                   "rows_total": None})
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        now = datetime.now(UTC)
        self._set({"finished_at": now, "updated_at": now, "outcome": "failed" if exc_type else "done"})

    def __call__(self, label: str, done: int, total: int) -> None:
        now = datetime.now(UTC)
        fields: dict[str, Any] = {"updated_at": now, "stage_index": done, "stage_total": total}
        if counted := _COUNTED.fullmatch(label):
            fields |= {"label": counted["label"], "rows_done": int(counted["done"]),
                       "rows_total": int(counted["total"])}
        else:
            fields |= {"stage": label, "stage_started_at": now, "label": None, "rows_done": None,
                       "rows_total": None}
        self._set(fields)

    def _set(self, fields: dict[str, Any]) -> None:
        # Watching is optional: a failed write must never stop the run.
        try:
            self.rows.update_one(self.key, {"$set": fields}, upsert=True)
        except PyMongoError as exc:
            logger.debug("progress record not written: %s", exc)
