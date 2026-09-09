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
    """Builds all seven edge types at once - holds the shared context
    (db/authorship/assignment/table/layout) as state instead of a parameter
    on every individual method."""

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

        # The first three take REAL weights from layout (not the
        # layout-only inflated ones - see the Layout docstring for why
        # those are different things); coauth/pub have a minimum-weight
        # threshold (EDGE_THRESHOLDS), repo has none - there aren't many
        # repo edges to begin with.
        coauth_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.coauth.items() if w >= EDGE_THRESHOLDS.coauth_min_w
        ]
        pub_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.pub_pair_w.items() if w >= EDGE_THRESHOLDS.pub_edge_min_w
        ]
        repo_edges = [{"s": a, "t": b, "w": w} for (a, b), w in layout.repo_edge_w.items()]

        # Department-to-department edges: how many publications connect a
        # pair of departments through shared authors. table.no_dept_gid is
        # dropped from the set BEFORE combinations - "no department" isn't a
        # department, an edge to it would be meaningless.
        # sorted() - pub_ids is a set; without this, insertion order into
        # dept_pair_w (and so the final dept_edges list order) depends on
        # per-process string hash randomization, producing a different byte
        # sequence in graph-data.json on every run even with the same seed
        # and the same input snapshot (content is identical, just reordered).
        dept_pair_w: dict[tuple[int, int], int] = defaultdict(int)
        for pid in sorted(authorship.pub_ids):
            ds = sorted({table.g(assignment.author_dept[per]) for per in authorship.pub_authors[pid]} - {table.no_dept_gid})
            for a, b in combinations(ds, 2):
                dept_pair_w[(a, b)] += 1
        dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

        # Author-repository (CONTRIBUTED_TO) - only for authors who actually
        # made it into the graph (static_depts covers every person in the
        # snapshot); the filter is a silent guard against a data mismatch,
        # not the expected case.
        repo_author_edges = [
            {"s": row["rid"], "t": row["per"], "role": row["role"]}
            for row in db["repo_persons"]
            if row["per"] in assignment.static_depts
        ]
        # Repository-publication (IMPLEMENTS) - only for publications with
        # at least one ITMO author (pub_ids); the rest never make it into the
        # graph at all.
        repo_pub_edges = [
            {"s": row["rid"], "t": row["pid"]} for row in db["repo_pubs"] if row["pid"] in authorship.pub_ids
        ]
        # Author-publication directly (AUTHORED) - the only edge with no
        # pub_ids filter: db["authorship"] already contains only ITMO
        # authorship, filtered by the Cypher query itself
        # (MATCH (p:Person {is_itmo: true})-[:AUTHORED]->...), not here in Python.
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
