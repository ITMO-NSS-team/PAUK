"""Keeps and reads the last health run.

The checks live in `pauk.admin.checks` and are run by `pauk.admin.graph_stats`.
They are too heavy to run on every page open, so a run saves its answer and the
page reads it. The rows behind a check are not saved: they are queried fresh.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from pauk.admin.graph_stats import collect_examples

logger = logging.getLogger("pauk.admin")

COLLECTION = "health"
#: One document: the panel shows the last answer, not a history of answers.
LATEST = "latest"

#: Worst first: the page is opened to find what is wrong.
ORDER = {"error": 0, "fail": 1, "warn": 2, "ok": 3}

WORDS = {
    "error": "не выполнилась",
    "fail": "плохо",
    "warn": "внимание",
    "ok": "в порядке",
}


def rows_behind(client, check_id: str, limit: int) -> dict:
    """The records behind one check, asked of the graph now.

    Reaches for the raw driver because the queries are Cypher written in
    `pauk.admin.checks`, which the client's label/field whitelist cannot
    express. Kept in one place so the exception stays visible.

    Raises:
        KeyError: No such check.
        ValueError: The check has no query for its rows.
    """
    return collect_examples(client.driver, check_id, limit)


def save(db: Database, stats: dict) -> dict:
    """Keep the answer a run just worked out."""
    document = {"_id": LATEST, "computed_at": datetime.now(UTC).isoformat(), "stats": stats}
    db[COLLECTION].replace_one({"_id": LATEST}, document, upsert=True)
    logger.info("health: %d check(s) recorded", len(stats.get("checks") or []))
    return document


def latest(db: Database) -> dict | None:
    """The last answer, or None when nobody has asked yet."""
    return db[COLLECTION].find_one({"_id": LATEST})


def verdict(checks: list[dict]) -> dict[str, int]:
    """How many checks are in each state, for the line above the list."""
    counted = dict.fromkeys(ORDER, 0)
    for check in checks:
        counted[check.get("status", "ok")] = counted.get(check.get("status", "ok"), 0) + 1
    return counted


def grouped(checks: list[dict]) -> list[dict]:
    """The checks as the page lays them out: by group, worst first.

    Inside a group the share decides, then the raw count.
    """
    order: list[str] = []
    by_group: dict[str, list[dict]] = {}
    for check in checks:
        group = check.get("group", "")
        if group not in by_group:
            order.append(group)
            by_group[group] = []
        by_group[group].append(check)
    return [
        {"name": group,
         "checks": sorted(by_group[group], key=_worst_first),
         "verdict": verdict(by_group[group])}
        for group in order
    ]


def _worst_first(check: dict) -> tuple[Any, ...]:
    return (ORDER.get(check.get("status", "ok"), 9),
            -(check.get("pct") or 0),
            -(check.get("n") or 0),
            check.get("title", ""))


def openable(check: dict) -> bool:
    """Whether there is anything to show behind this check."""
    return bool(check.get("has_examples")) and bool(check.get("n")) \
        and check.get("status") != "error"
