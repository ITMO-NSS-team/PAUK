"""Compatibility bridge for the manual-review panel from PR #177.

The resolver is independently mergeable.  When the review storage module is
present, held pairs are written to its queue and previous answers override
automatic decisions.  Until PR #177 lands, the existing JSONL audit remains
the fallback and the pipeline keeps running.
"""

from __future__ import annotations

from importlib import import_module

from pymongo.database import Database


def _backend():
    try:
        return import_module("pauk.storage.review")
    except ModuleNotFoundError as exc:
        if exc.name != "pauk.storage.review":
            raise
        return None


def decisions(db: Database, aliases: dict[str, str] | None = None) -> dict[frozenset[str], str]:
    backend = _backend()
    return backend.decisions(db, aliases) if backend is not None else {}


def staff_choices(db: Database, aliases: dict[str, str] | None = None) -> dict[str, str]:
    backend = _backend()
    return backend.staff_choices(db, aliases) if backend is not None else {}


def record(db: Database, report: list[dict], source: str) -> None:
    backend = _backend()
    if backend is None:
        return
    backend.record_held(db, report, source=source)
    backend.record_disputed(db, report)


def mark_applied(db: Database, aliases: dict[str, str]) -> None:
    backend = _backend()
    if backend is not None:
        backend.mark_applied_merges(db, aliases)
