"""Assigns a department to every author/publication/repository - rules in
`docs/architecture/gui.md`: a publication gets the majority vote among its
ITMO authors; an author gets their most recent publication's department; a
repository gets the majority among the departments of the publications it
implements. Ties break by id, not by global popularity (otherwise large
departments would only keep growing themselves).
"""

from __future__ import annotations

import colorsys
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .authorship import Authorship
from .config import NO_DEPT_COLOR, NO_DEPT_NAME, NO_DEPT_NAME_EN

logger = logging.getLogger(__name__)


def golden_color(i: int) -> str:
    """Department color: step by the golden ratio for hue, HLS l=0.6 s=0.4.

    Args:
        i: Department's ordinal (after reindexing by size).

    Returns:
        Color as `#rrggbb`.

    Example:
        >>> golden_color(0)
        '#c27070'
    """
    hue = (i * 0.618033988749895) % 1.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.6, 0.4)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def majority_dept(dept_lists: Iterable[Iterable[str]]) -> str | None:
    """Department by majority vote; ties break by id, not by global
    popularity - that would create a "rich get richer" feedback loop
    favoring already-large departments.

    Args:
        dept_lists: A list of departments per "voter" (e.g. each coauthor's
            department list for one publication).

    Returns:
        The winning department id, or `None` if there were no votes at all.

    Example:
        >>> majority_dept([["d1"], ["d1", "d2"], ["d2"]])
        'd1'
    """
    cnt: Counter[str] = Counter()
    for depts in dept_lists:
        for d in depts:
            cnt[d] += 1
    if not cnt:
        return None
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


@dataclass(frozen=True)
class DepartmentAssignment:
    """Each author/publication/repository's department."""

    static_depts: dict[str, list[str]]
    """Author -> the departments they actually belong to (BELONGS_TO)."""
    pub_dept_rows: dict[str, list[str]]
    """Publication -> its full department list (PRODUCED_BY), not just the primary one."""
    pub_primary: dict[str, str | None]
    """Publication -> its primary department (majority vote among authors, falls back to PRODUCED_BY)."""
    author_dept: dict[str, str | None]
    """Author -> the department of their most recent publication."""
    repo_pub_map: dict[str, list[str]]
    """Repository -> the publications it implements (within pub_ids)."""
    repo_dept_rows: dict[str, list[str]]
    """Repository -> its full department list (DEVELOPED_BY)."""
    repo_dept: dict[str, str | None]
    """Repository -> its primary department."""


@dataclass(frozen=True)
class DepartmentTable:
    """The final department table for the map, plus the id-compaction function."""

    departments: list[dict]
    """Rows for `graph-data.json["departments"]`, sorted by size, "no department" last."""
    g: Callable[[str | None], int]
    """Graph department id -> dense frontend id (or the "no department" bucket id, if department is falsy)."""
    no_dept_gid: int
    """Dense id of the "no department" bucket."""


class DepartmentAssigner:
    """Assigns a department to every entity and builds the final table for
    the frontend - two methods instead of two free functions, because both
    share the same context (`db`/`authorship`) and graph_builder.py calls
    them back to back exactly once per run.
    """

    def __init__(self, db: dict[str, list[dict]], authorship: Authorship) -> None:
        self.db = db
        self.authorship = authorship

    def assign(self, dept_name: dict[str, str]) -> DepartmentAssignment:
        """Assigns a department to every author/publication/repository.

        Args:
            dept_name: Department id -> display name (used only as a "this
                department exists" filter, not for the name itself).

        Returns:
            `DepartmentAssignment` with every intermediate and final assignment.
        """
        db, authorship = self.db, self.authorship

        # Step 1: BELONGS_TO as-is - every real department of every author
        # (can be several, can be none). A key is created for EVERY author in
        # persons (even with zero edges) - otherwise a KeyError below, in the
        # author_dept loop, for an author with no BELONGS_TO at all.
        static_depts: dict[str, list[str]] = {row["id"]: [] for row in db["persons"]}
        for row in db["person_depts"]:
            per, did = row["per"], row["did"]
            if did in dept_name:  # silently drop an edge to a nonexistent/deleted department
                static_depts[per].append(did)

        # Step 2: PRODUCED_BY as-is - a publication's full department list,
        # not just the primary one (computed below, in pub_primary). Filtered
        # by pub_ids - publications with no ITMO author never reach here at all.
        pub_dept_rows: dict[str, list[str]] = defaultdict(list)
        for row in db["pub_depts"]:
            pid, did = row["pid"], row["did"]
            if pid in authorship.pub_ids and did in dept_name and did not in pub_dept_rows[pid]:
                pub_dept_rows[pid].append(did)

        # Step 3: a publication's primary department - majority vote among
        # its ITMO authors (by their static_depts), falling back to the first
        # PRODUCED_BY if there were no votes at all (authors with zero BELONGS_TO).
        pub_primary: dict[str, str | None] = {}
        for pid in authorship.pub_ids:
            primary = majority_dept(static_depts.get(per, []) for per in authorship.pub_authors[pid])
            if primary is None and pub_dept_rows.get(pid):
                primary = pub_dept_rows[pid][0]
            pub_primary[pid] = primary

        # Step 4: an author's department - the primary department of their
        # MOST RECENT publication (sorted by date descending, take the first
        # one that actually has a pub_primary - older publications with no
        # department are skipped, not treated as a dead end).
        pub_date = {r["id"]: (r["publication_date"] or "") for r in authorship.pubs_rows}
        author_dept: dict[str, str | None] = {}
        for per in static_depts:
            dept = None
            for pid in sorted(authorship.author_pubs.get(per, []), key=lambda p: (pub_date.get(p, ""), p), reverse=True):
                if pub_primary.get(pid):
                    dept = pub_primary[pid]
                    break
            author_dept[per] = dept

        # Step 5: repository -> the publications it implements (IMPLEMENTS,
        # see edges.py), within pub_ids - needed below to compute a
        # repository's department from THOSE publications' departments.
        repo_pub_map: dict[str, list[str]] = defaultdict(list)
        for row in db["repo_pubs"]:
            rid, pid = row["rid"], row["pid"]
            if pid in authorship.pub_ids:
                repo_pub_map[rid].append(pid)
        # DEVELOPED_BY as-is - the same role for a repository that
        # PRODUCED_BY plays for a publication (full list, not just primary).
        repo_dept_rows: dict[str, list[str]] = defaultdict(list)
        for row in db["repo_depts"]:
            rid, did = row["rid"], row["did"]
            if did in dept_name and did not in repo_dept_rows[rid]:
                repo_dept_rows[rid].append(did)

        # Step 6: a repository's primary department - majority vote among the
        # departments of the publications it implements (via repo_pub_map +
        # pub_primary), falling back to DEVELOPED_BY the same way publications do.
        repo_dept: dict[str, str | None] = {}
        for row in db["repositories"]:
            rid = row["id"]
            primary = majority_dept(
                [dept] for p in repo_pub_map.get(rid, []) if (dept := pub_primary.get(p))
            )
            if primary is None and repo_dept_rows.get(rid):
                primary = repo_dept_rows[rid][0]
            repo_dept[rid] = primary

        return DepartmentAssignment(
            static_depts=static_depts,
            pub_dept_rows=dict(pub_dept_rows),
            pub_primary=pub_primary,
            author_dept=author_dept,
            repo_pub_map=dict(repo_pub_map),
            repo_dept_rows=dict(repo_dept_rows),
            repo_dept=repo_dept,
        )

    def build_table(
        self, dept_name: dict[str, str], dept_name_en: dict[str, str], assignment: DepartmentAssignment
    ) -> DepartmentTable:
        """Counts how many entities each department has, sorts by size, and
        reindexes into dense ids 0..N (plus a separate "no department"
        bucket last) - so the frontend never needs to know the graph's real
        (arbitrary) department ids.

        Args:
            dept_name: Department id -> Russian name.
            dept_name_en: Department id -> English name.
            assignment: Result of `assign()`.

        Returns:
            `DepartmentTable`.
        """
        db, authorship = self.db, self.authorship

        # Count how many times each department was SOMEONE's primary
        # (author_dept/pub_primary/repo_dept) - this sum drives the sort
        # order below: larger departments get a smaller (more prominent) dense id.
        usage: Counter[str] = Counter()
        for d in assignment.author_dept.values():
            if d:  # None means "this entity has no department", not a vote
                usage[d] += 1
        for d in assignment.pub_primary.values():
            if d:
                usage[d] += 1
        for d in assignment.repo_dept.values():
            if d:
                usage[d] += 1
        # += 0, not just "remember the id set": a department that only ever
        # shows up as secondary (PRODUCED_BY/DEVELOPED_BY), never as anyone's
        # primary, must still get a key in usage - otherwise it's missing from
        # gid below, and table.g() raises KeyError the first time it's
        # referenced as a non-primary department of a publication/repository.
        for rows in (assignment.pub_dept_rows, assignment.repo_dept_rows):
            for depts in rows.values():
                for d in depts:
                    usage[d] += 0

        # Sorted by usage descending, ties broken by name (deterministic, not
        # dict iteration order). gid - dense ids 0..N-1 in this order;
        # no_dept_gid - the id right after the last real department, for the
        # "no department" bucket.
        ordered = sorted(usage, key=lambda d: (-usage[d], dept_name[d]))
        gid = {d: i for i, d in enumerate(ordered)}
        no_dept_gid = len(ordered)

        def g(dept_db_id: str | None) -> int:
            # Falsy (None or "") -> the "no department" bucket, not a KeyError.
            return gid[dept_db_id] if dept_db_id else no_dept_gid

        # Counts authors/publications/repositories for EVERY dense id at once
        # (via Counter), rather than one loop per count - these three numbers
        # go straight into the n_authors/n_pubs/n_repos fields below.
        n_auth = Counter(g(d) for d in assignment.author_dept.values())
        n_pub = Counter(g(assignment.pub_primary[p]) for p in authorship.pub_ids)
        n_repo = Counter(g(assignment.repo_dept[r["id"]]) for r in db["repositories"])

        # One row per real department, in sorted order (ordered), with dense
        # id/color/the three counts.
        departments = [
            {
                "id": gid[d],
                "name": dept_name[d],
                "name_en": dept_name_en[d],
                "color": golden_color(gid[d]),
                "n": n_auth[gid[d]] + n_pub[gid[d]] + n_repo[gid[d]],
                "n_authors": n_auth[gid[d]],
                "n_pubs": n_pub[gid[d]],
                "n_repos": n_repo[gid[d]],
            }
            for d in ordered
        ]
        # Plus one extra row - the synthetic "no department" bucket (id/name/
        # color from config.py, not from any real graph department), always last.
        departments.append(
            {
                "id": no_dept_gid,
                "name": NO_DEPT_NAME,
                "name_en": NO_DEPT_NAME_EN,
                "color": NO_DEPT_COLOR,
                "n": n_auth[no_dept_gid] + n_pub[no_dept_gid] + n_repo[no_dept_gid],
                "n_authors": n_auth[no_dept_gid],
                "n_pubs": n_pub[no_dept_gid],
                "n_repos": n_repo[no_dept_gid],
            }
        )
        logger.info('Departments: %d (+ "%s")', len(ordered), NO_DEPT_NAME)
        return DepartmentTable(departments=departments, g=g, no_dept_gid=no_dept_gid)
