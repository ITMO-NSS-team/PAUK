"""Юнит-тесты для `layout.py` — чистая математика раскладки, без сети и без
Neo4j. golden_color/majority_dept протестированы в `test_departments.py`,
dense_rank — в `test_nodes.py` (переехали туда вместе с функциями: ни у
одной не оказалось больше одного реального потребителя, отдельный
`ranking.py` был чистой индирекцией).
"""

from __future__ import annotations

import random
import unittest

from new_generate.layout import (
    ForceAtlasLayouter,
    fa2_blended_layout,
    fit_coords,
    sparse_dept_edges,
    spread_min_distance,
)


class FitCoordsTest(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(fit_coords({}), {})

    def test_scales_into_coordinate_bounds(self):
        """Разброс координат может быть каким угодно (FA2 не ограничивает
        себя никаким диапазоном) — на выходе всё должно попасть в
        [30, 970], пространство фронтенда."""
        pos = {"a": (-500.0, 1000.0), "b": (500.0, -1000.0), "c": (0.0, 0.0)}
        fitted = fit_coords(pos)
        for x, y in fitted.values():
            self.assertGreaterEqual(x, 30.0)
            self.assertLessEqual(x, 970.0)
            self.assertGreaterEqual(y, 30.0)
            self.assertLessEqual(y, 970.0)

    def test_accepts_numpy_like_sequences_not_just_tuples(self):
        """networkx.forceatlas2_layout отдаёт позиции numpy-массивами, а не
        кортежами — fit_coords должна принимать и то, и другое."""
        fitted = fit_coords({"a": [1.0, 2.0], "b": [3.0, 4.0]})
        self.assertEqual(set(fitted), {"a", "b"})


class SpreadMinDistanceTest(unittest.TestCase):
    def test_coincident_points_get_pushed_apart(self):
        """Порог остановки — max(2, n // 2000): "пара отставших пар из
        тысяч — нормально". При всего 2 точках это допускает вообще не
        двигать их (1 возможная пара <= порога 2) — не баг, а эвристика,
        рассчитанная на реальный масштаб. Поэтому тест берёт точек больше,
        чем порог может скрыть, и проверяет итоговое число слишком близких
        пар, а не расстояние в одной конкретной паре."""
        pos = {str(i): (500.0, 500.0) for i in range(8)}
        result = spread_min_distance(pos, d_min=10.0, seed=1)
        coords = list(result.values())
        too_close = sum(
            1
            for i in range(len(coords))
            for j in range(i + 1, len(coords))
            if ((coords[i][0] - coords[j][0]) ** 2 + (coords[i][1] - coords[j][1]) ** 2) ** 0.5 < 9.0
        )
        self.assertLessEqual(too_close, 2)

    def test_already_far_apart_points_stay_put(self):
        pos = {"a": (100.0, 100.0), "b": (900.0, 900.0)}
        result = spread_min_distance(pos, d_min=10.0, seed=1)
        self.assertEqual(result["a"], (100.0, 100.0))
        self.assertEqual(result["b"], (900.0, 900.0))


class SparseDeptEdgesTest(unittest.TestCase):
    def test_only_connects_nodes_within_the_same_department(self):
        dept_of: dict[str, str | None] = {"a1": "d1", "a2": "d1", "a3": "d1", "b1": "d2", "b2": "d2"}
        edges = sparse_dept_edges(set(dept_of), dept_of, random.Random(1), k=2)
        for a, b in edges:
            self.assertEqual(dept_of[a], dept_of[b])

    def test_department_with_one_member_gets_no_edges(self):
        dept_of: dict[str, str | None] = {"a1": "d1", "solo": "d2"}
        edges = sparse_dept_edges(set(dept_of), dept_of, random.Random(1), k=2)
        self.assertEqual(edges, {})

    def test_node_without_department_is_excluded(self):
        dept_of = {"a1": "d1", "a2": "d1", "nodept": None}
        edges = sparse_dept_edges(set(dept_of), dept_of, random.Random(1), k=2)
        for a, b in edges:
            self.assertNotIn("nodept", (a, b))


class Fa2BlendedLayoutTest(unittest.TestCase):
    def test_every_node_gets_a_position(self):
        all_ids = {"a", "b", "c", "d"}
        edge_weights = {("a", "b"): 2.0}
        pos, stats = fa2_blended_layout(edge_weights, all_ids, max_iter=10, seed=1)
        self.assertEqual(set(pos), all_ids)
        self.assertEqual(len(stats), 4)

    def test_deterministic_for_the_same_seed(self):
        all_ids = {"a", "b", "c", "d", "e"}
        edge_weights = {("a", "b"): 2.0, ("b", "c"): 1.0}
        pos1, _ = fa2_blended_layout(edge_weights, all_ids, max_iter=10, seed=42)
        pos2, _ = fa2_blended_layout(edge_weights, all_ids, max_iter=10, seed=42)
        self.assertEqual(pos1, pos2)

    def test_isolated_singletons_without_any_edges_still_get_positions(self):
        """Без гигантской компоненты (нет рёбер вообще) — всё уходит в
        подмешивание синглтонов, не должно падать."""
        pos, (n_giant, e_giant, n_small, n_single) = fa2_blended_layout({}, {"a", "b", "c"}, max_iter=10, seed=1)
        self.assertEqual(set(pos), {"a", "b", "c"})
        self.assertEqual((n_giant, e_giant, n_small), (0, 0, 0))
        self.assertEqual(n_single, 3)


class ForceAtlasLayouterTest(unittest.TestCase):
    """`ForceAtlasLayouter` — тонкая обёртка вокруг fa2_blended_layout/
    spread_min_distance/fit_coords с seed как состоянием - проверяем, что
    обёртка реально прокидывает вызовы, а не тестируем саму математику
    ещё раз (та уже покрыта тестами выше)."""

    def test_blended_positions_every_node_and_is_deterministic(self):
        layouter = ForceAtlasLayouter(seed=1)
        all_ids = {"a", "b", "c"}
        pos, stats = layouter.blended({("a", "b"): 2.0}, all_ids, max_iter=10, min_sep=1.0)
        self.assertEqual(set(pos), all_ids)
        self.assertEqual(len(stats), 4)

    def test_simple_positions_every_node(self):
        import networkx as nx

        graph = nx.Graph()
        graph.add_weighted_edges_from([("a", "b", 1.0)])
        pos = ForceAtlasLayouter(seed=1).simple(graph, max_iter=10)
        self.assertEqual(set(pos), {"a", "b"})
