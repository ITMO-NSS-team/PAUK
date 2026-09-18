"""The graph's own health, as the panel keeps and reads it.

The checks themselves live in `pauk.gui.checks` and are run by
`pauk.gui.generate_stats` — one set of definitions for the map's tab and
for this page, because two would answer differently about the same graph
within a month.

What is here is the part the panel needs and the map does not: somewhere to
keep the last answer. Thirty-two checks are thirty-two counts plus their
denominators, several of them regex scans over every person, and a page
that ran them on every open would be a page nobody opens twice. So a run
writes the answer down and the page reads it, with the time it was taken
shown next to it — a number without its date is worse than no number.

The rows behind a check are not kept: they are a `LIMIT`-ed query, cheap
enough to run while somebody looks at them, and stale examples of a problem
that has since been fixed would be the wrong kind of wrong.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from pauk.gui.generate_stats import collect_examples

logger = logging.getLogger("pauk.admin")

COLLECTION = "health"
#: One document: the panel shows the last answer, not a history of answers.
#: What changed over time is a different question, and the journal of runs
#: already records when each was taken.
LATEST = "latest"

#: Worst first. Somebody opening the page is looking for what is wrong, and
#: a list that opens on thirty green lines hides the three red ones.
ORDER = {"error": 0, "fail": 1, "warn": 2, "ok": 3}

WORDS = {
    "error": "не выполнилась",
    "fail": "плохо",
    "warn": "внимание",
    "ok": "в порядке",
}


def rows_behind(client, check_id: str, limit: int) -> dict:
    """The records behind one check, asked of the graph now.

    Reaches for the raw driver, which routes otherwise never do. The reason
    is that these queries are not the panel's: they are written in
    `pauk.gui.checks` as Cypher, against the whole graph, and the client's
    whitelist of labels and fields has nothing to offer them. Kept in one
    place so the reach is visible and explained rather than repeated.

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

    Inside a group the share decides, then the raw count — a check failing
    on a tenth of the graph matters more than one failing on three rows,
    and neither is worth reading before the one that failed outright.
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
    """Whether there is anything to show behind this check.

    A check with no rows has nothing to open, and one that did not run has
    nothing to show either — the link would lead to the same error twice.
    """
    return bool(check.get("has_examples")) and bool(check.get("n")) \
        and check.get("status") != "error"
