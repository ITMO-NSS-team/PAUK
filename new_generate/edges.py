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
        """Строит все семь типов рёбер для `graph-data.json`.

        Возвращает:
            Словарь с ключами `coauth_edges`/`pub_edges`/`repo_edges`/
            `dept_edges`/`repo_author_edges`/`repo_pub_edges`/`all_edges`.
        """
        db, authorship, assignment, table, layout = self.db, self.authorship, self.assignment, self.table, self.layout

        # Первые три ребра берут РЕАЛЬНЫЕ веса из layout (не layout-only
        # инфляцию — см. докстринг Layout про то, почему это разные вещи),
        # coauth/pub с порогом на слабые связи (EDGE_THRESHOLDS), repo — без
        # порога, там связей и так немного.
        coauth_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.coauth.items() if w >= EDGE_THRESHOLDS.coauth_min_w
        ]
        pub_edges = [
            {"s": a, "t": b, "w": w} for (a, b), w in layout.pub_pair_w.items() if w >= EDGE_THRESHOLDS.pub_edge_min_w
        ]
        repo_edges = [{"s": a, "t": b, "w": w} for (a, b), w in layout.repo_edge_w.items()]

        # Рёбра между департаментами: сколько публикаций связывает пару
        # департаментов через общих авторов. table.no_dept_gid убран из
        # множества ДО combinations — "без департамента" не департамент,
        # ребро с ним никакого смысла не несёт.
        dept_pair_w: dict[tuple[int, int], int] = defaultdict(int)
        for pid in authorship.pub_ids:
            ds = sorted({table.g(assignment.author_dept[per]) for per in authorship.pub_authors[pid]} - {table.no_dept_gid})
            for a, b in combinations(ds, 2):
                dept_pair_w[(a, b)] += 1
        dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

        # Автор-репозиторий (CONTRIBUTED_TO) — только для авторов, которые
        # реально попали в граф (static_depts — все персоны из снепшота);
        # фильтр молча защищает от рассинхрона данных, а не от ожидаемого случая.
        repo_author_edges = [
            {"s": row["rid"], "t": row["per"], "role": row["role"]}
            for row in db["repo_persons"]
            if row["per"] in assignment.static_depts
        ]
        # Репозиторий-публикация (IMPLEMENTS) — только для публикаций с хотя
        # бы одним ИТМО-автором (pub_ids), остальные в граф не попадают вовсе.
        repo_pub_edges = [
            {"s": row["rid"], "t": row["pid"]} for row in db["repo_pubs"] if row["pid"] in authorship.pub_ids
        ]
        # Автор-публикация напрямую (AUTHORED) — единственное ребро без
        # фильтра по authorship.pub_ids: db["authorship"] и так содержит
        # только ИТМО-авторство — это уже отфильтровано в самом Cypher-запросе
        # new_cache (MATCH (p:Person {is_itmo: true})-[:AUTHORED]->...), а не
        # где-то здесь, в Python.
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
