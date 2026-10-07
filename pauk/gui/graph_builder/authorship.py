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
    """Publication -> list of all its authors, ITMO and external."""
    author_pubs: dict[str, list[str]]
    """Author -> list of their publications (the ones in pub_authors)."""
    pubs_rows: list[dict]
    """Rows from db["publications"], filtered down to pub_ids."""
    pub_ids: set[str]
    """ids of publications with at least one ITMO author."""
    external_ids: frozenset[str] = frozenset()
    """Authors with `is_itmo` false - shown on the map like everyone else, just hidden by default."""


def build_authorship_index(db: dict[str, list[dict]]) -> Authorship:
    """Builds the authorship index and drops publications with no ITMO author.

    A person counts as external only when the snapshot explicitly says
    `is_itmo: false` - snapshots exported before external authors were
    included carry no such field and only ever held ITMO people.

    Args:
        db: Graph snapshot in the shape `pauk.cache.export::load_db()` returns.

    Returns:
        `Authorship` with indexes both ways and the filtered publication list.
    """
    external_ids = frozenset(row["id"] for row in db.get("persons", []) if row.get("is_itmo") is False)
    pub_authors: dict[str, list[str]] = defaultdict(list)
    for row in db["authorship"]:
        pub_authors[row["pid"]].append(row["per"])
    pub_authors = {pid: pers for pid, pers in pub_authors.items() if any(p not in external_ids for p in pers)}

    author_pubs: dict[str, list[str]] = defaultdict(list)
    for pid, pers in pub_authors.items():
        for per in pers:
            author_pubs[per].append(pid)

    pubs_rows = [r for r in db["publications"] if r["id"] in pub_authors]
    pub_ids = {r["id"] for r in pubs_rows}
    logger.info("Publications with ITMO authors: %d of %d", len(pubs_rows), len(db["publications"]))
    return Authorship(pub_authors, dict(author_pubs), pubs_rows, pub_ids, external_ids)
