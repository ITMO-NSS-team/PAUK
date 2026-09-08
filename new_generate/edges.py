"""Сборка всех семи типов рёбер для `graph-data.json`."""

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
    """Строит все семь типов рёбер разом — держит общий контекст
    (db/authorship/assignment/table/layout) как состояние вместо параметра
    в каждом отдельном методе."""

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
        """Возвращает:
            Словарь с ключами `coauth_edges`/`pub_edges`/`repo_edges`/
            `dept_edges`/`repo_author_edges`/`repo_pub_edges`/`all_edges`.
        """
        db, authorship, assignment, table, layout = self.db, self.authorship, self.assignment, self.table, self.layout

        coauth_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.coauth.items() if w >= EDGE_THRESHOLDS.coauth_min_w
        ]
        pub_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.pub_pair_w.items() if w >= EDGE_THRESHOLDS.pub_edge_min_w
        ]
        repo_edges = [{"s": a, "t": b, "w": w} for (a, b), w in layout.repo_edge_w.items()]

        dept_pair_w: dict[tuple[int, int], int] = defaultdict(int)
        for pid in authorship.pub_ids:
            ds = sorted({table.g(assignment.author_dept[per]) for per in authorship.pub_authors[pid]} - {table.no_dept_gid})
            for a, b in combinations(ds, 2):
                dept_pair_w[(a, b)] += 1
        dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

        repo_author_edges = [
            {"s": row["rid"], "t": row["per"], "role": row["role"]}
            for row in db["repo_persons"]
            if row["per"] in assignment.static_depts
        ]
        repo_pub_edges = [
            {"s": row["rid"], "t": row["pid"]} for row in db["repo_pubs"] if row["pid"] in authorship.pub_ids
        ]
        all_edges = [{"s": row["per"], "t": row["pid"]} for row in db["authorship"]]

        logger.info(
            "Рёбра: coauth %d, pub %d, repo %d, dept %d, repo-author %d, repo-pub %d, authorship %d",
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
