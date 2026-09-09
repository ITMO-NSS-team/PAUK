"""Authorship index: who wrote what, which publications even make it into the graph.

Called once per run, never reused with a different config - a plain
function rather than a class, nothing to hold as state between calls.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Authorship:
    """Who wrote what: the authorship index and publications, filtered down
    to those with at least one ITMO author."""

    pub_authors: dict[str, list[str]]
    """Publication -> list of its ITMO authors."""
    author_pubs: dict[str, list[str]]
    """Author -> list of their publications (the ones in pub_authors)."""
    pubs_rows: list[dict]
    """Rows from db["publications"], filtered down to pub_ids."""
    pub_ids: set[str]
    """ids of publications with at least one ITMO author."""


def build_authorship_index(db: dict[str, list[dict]]) -> Authorship:
    """Builds the authorship index and drops publications with no ITMO author.

    Args:
        db: Graph snapshot in the shape `pauk.cache.export::load_db()` returns.

    Returns:
        `Authorship` with indexes both ways and the filtered publication list.
    """
    pub_authors: dict[str, list[str]] = defaultdict(list)
    author_pubs: dict[str, list[str]] = defaultdict(list)
    for row in db["authorship"]:
        pid, per = row["pid"], row["per"]
        pub_authors[pid].append(per)
        author_pubs[per].append(pid)

    pubs_rows = [r for r in db["publications"] if r["id"] in pub_authors]
    pub_ids = {r["id"] for r in pubs_rows}
    logger.info("Publications with ITMO authors: %d of %d", len(pubs_rows), len(db["publications"]))
    return Authorship(dict(pub_authors), dict(author_pubs), pubs_rows, pub_ids)
