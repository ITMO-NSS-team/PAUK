"""Builds all seven edge types for `graph-data.json`."""

from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations

from .authorship import Authorship
from .config import EDGE_THRESHOLDS
from .departments import DepartmentAssignment, DepartmentTable
from .layout import Layout

logger = logging.getLogger(__name__)


class EdgeBuilder:
    """Builds all seven edge types at once, holding the shared context as state."""

    def __init__(
        self,
        db: dict[str, list[dict]],
        authorship: Authorship,
        assignment: DepartmentAssignment,
        table: DepartmentTable,
        layout: Layout,
    ) -> None:
        self.db = db
        self.authorship = authorship
        self.assignment = assignment
        self.table = table
        self.layout = layout

    def build(self) -> dict[str, list[dict]]:
        """Builds all seven edge types for `graph-data.json`.

        Returns:
            A dict with keys `coauth_edges`/`pub_edges`/`repo_edges`/
            `dept_edges`/`repo_author_edges`/`repo_pub_edges`/`all_edges`.
        """
        db, authorship, assignment, table, layout = self.db, self.authorship, self.assignment, self.table, self.layout

        # The first three use the real weights from layout, not the inflated
        # layout-only ones (see the Layout docstring). Repo edges have no
        # threshold since there are few of them.
        coauth_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.coauth.items() if w >= EDGE_THRESHOLDS.coauth_min_w
        ]
        pub_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.pub_pair_w.items() if w >= EDGE_THRESHOLDS.pub_edge_min_w
        ]
        repo_edges = [
            {"s": a, "t": b, "w": round(w, 2), "via": layout.repo_edge_via[(a, b)]}
            for (a, b), w in layout.repo_edge_w.items()
        ]

        # Department-to-department edges: publications connecting a pair through
        # shared authors. "No department" is dropped before combinations, since
        # an edge to it is meaningless. sorted() because pub_ids is a set, and
        # hash randomization would otherwise reorder dept_edges between runs.
        dept_pair_w: dict[tuple[int, int], int] = defaultdict(int)
        for pid in sorted(authorship.pub_ids):
            itmo_authors = [per for per in authorship.pub_authors[pid] if per not in authorship.external_ids]
            ds = sorted({table.g(assignment.author_dept[per]) for per in itmo_authors} - {table.no_dept_gid})
            for a, b in combinations(ds, 2):
                dept_pair_w[(a, b)] += 1
        dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

        # Author-repository (CONTRIBUTED_TO); the filter is a silent guard
        # against a data mismatch, not the expected case.
        repo_author_edges = [
            {"s": row["rid"], "t": row["per"], "role": row["role"]}
            for row in db["repo_persons"]
            if row["per"] in assignment.static_depts
        ]
        # Repository-publication (IMPLEMENTS), only for publications in pub_ids.
        repo_pub_edges = [
            {"s": row["rid"], "t": row["pid"]} for row in db["repo_pubs"] if row["pid"] in authorship.pub_ids
        ]
        # Author-publication (AUTHORED), ITMO and external alike; db["authorship"]
        # is already cut down to pub_ids in builder.py.
        all_edges = [{"s": row["per"], "t": row["pid"]} for row in db["authorship"]]

        logger.info(
            "Edges: coauth %d, pub %d, repo %d, dept %d, repo-author %d, repo-pub %d, authorship %d",
            len(coauth_edges), len(pub_edges), len(repo_edges), len(dept_edges), len(repo_author_edges), len(repo_pub_edges), len(all_edges),
        )
        return {
            "coauth_edges": coauth_edges,
            "pub_edges": pub_edges,
            "repo_edges": repo_edges,
            "dept_edges": dept_edges,
            "repo_author_edges": repo_author_edges,
            "repo_pub_edges": repo_pub_edges,
            "all_edges": all_edges,
        }
