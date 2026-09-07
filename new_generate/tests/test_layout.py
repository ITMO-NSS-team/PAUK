"""Юнит-тесты для `layout.py` — чистая математика, без сети и без Neo4j.

Раньше в `pauk/gui/layout.py` не было ни одного теста, хотя все функции
здесь чистые (нет побочных эффектов) — идеальные кандидаты для юнит-тестов,
просто этим никто не занимался.
"""

from __future__ import annotations

import random
import unittest

from new_generate.layout import (
    dense_rank,
    fa2_blended_layout,
    fit_coords,
    golden_color,
    majority_dept,
    sparse_dept_edges,
    spread_min_distance,
)


class GoldenColorTest(unittest.TestCase):
    def test_returns_hex_color_format(self):
        self.assertRegex(golden_color(0), r"^#[0-9a-f]{6}$")

    def test_deterministic_and_distinct_for_different_indices(self):
        """Один и тот же индекс — всегда один и тот же цвет; соседние
        департаменты не должны случайно совпасть по цвету."""
        self.assertEqual(golden_color(5), golden_color(5))
        self.assertNotEqual(golden_color(0), golden_color(1))


class DenseRankTest(unittest.TestCase):
    def test_ties_get_equal_top_rank(self):
        self.assertEqual(dense_rank({"a": 1, "b": 5, "c": 5}), {"a": 0.5, "b": 1.0, "c": 1.0})

    def test_all_equal_values_all_rank_one(self):
        self.assertEqual(dense_rank({"a": 3, "b": 3}), {"a": 1.0, "b": 1.0})

    def test_single_value(self):
        self.assertEqual(dense_rank({"a": 10}), {"a": 1.0})


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


class MajorityDeptTest(unittest.TestCase):
    def test_majority_wins(self):
        self.assertEqual(majority_dept([["d1"], ["d1", "d2"], ["d2"]]), "d1")

    def test_tie_broken_by_id_not_by_global_popularity(self):
        """Именно поэтому нельзя сортировать по глобальной популярности
        департамента — только по id, иначе крупные департаменты подтягивали
        бы к себе все спорные случаи."""
        self.assertEqual(majority_dept([["dz"], ["da"]]), "da")

    def test_no_votes_returns_none(self):
        self.assertIsNone(majority_dept([]))
        self.assertIsNone(majority_dept([[], []]))
