"""Unit tests for layout.py - pure layout math, no network, no Neo4j."""

from __future__ import annotations

import random
import unittest

from pauk.gui.graph_builder.layout import (
    ForceAtlasLayouter,
    co_membership_weights,
    coauthor_pairs,
    fa2_blended_layout,
    fa2_layout,
    fit_coords,
    groups_of,
    place_external_authors,
    sparse_dept_edges,
    spread_min_distance,
    top_k_edges,
)


class FitCoordsTest(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(fit_coords({}), {})

    def test_scales_into_coordinate_bounds(self):
        """Coordinate spread can be anything (FA2 doesn't constrain itself
        to any range) - the output must land in [30, 970], the frontend space."""
        pos = {"a": (-500.0, 1000.0), "b": (500.0, -1000.0), "c": (0.0, 0.0)}
        fitted = fit_coords(pos)
        for x, y in fitted.values():
            self.assertGreaterEqual(x, 30.0)
            self.assertLessEqual(x, 970.0)
            self.assertGreaterEqual(y, 30.0)
            self.assertLessEqual(y, 970.0)

    def test_accepts_numpy_like_sequences_not_just_tuples(self):
        """Layout positions can arrive as numpy arrays, not tuples -
        fit_coords must accept both."""
        fitted = fit_coords({"a": [1.0, 2.0], "b": [3.0, 4.0]})
        self.assertEqual(set(fitted), {"a", "b"})


class SpreadMinDistanceTest(unittest.TestCase):
    def test_coincident_points_get_pushed_apart(self):
        """Stopping threshold is max(2, n // 2000): "a couple of straggler
        pairs out of thousands is fine". With only 2 points this allows not
        moving them at all (1 possible pair <= the threshold of 2) - not a
        bug, a heuristic tuned for real-world scale. So the test uses more
        points than the threshold can hide, and checks the final count of
        too-close pairs rather than the distance within one specific pair."""
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


class PlaceExternalAuthorsTest(unittest.TestCase):
    def test_external_lands_near_the_weighted_centroid_of_itmo_coauthors(self):
        pos = {"A1": (100.0, 100.0), "A2": (400.0, 100.0)}
        coauth = {("A1", "E1"): 2, ("A2", "E1"): 1}
        placed = place_external_authors(pos, coauth, frozenset({"E1"}), seed=1)
        x, y = placed["E1"]
        self.assertLess(abs(x - 200.0), 20)  # (2*100 + 1*400) / 3
        self.assertLess(abs(y - 100.0), 20)

    def test_itmo_positions_are_not_touched_and_seed_is_reproducible(self):
        pos = {"A1": (500.0, 500.0)}
        coauth = {("A1", f"E{i}"): 1 for i in range(50)}
        externals = frozenset(f"E{i}" for i in range(50))
        first = place_external_authors(pos, coauth, externals, seed=3)
        self.assertEqual(first, place_external_authors(pos, coauth, externals, seed=3))
        self.assertEqual(set(first), externals)
        self.assertEqual(pos, {"A1": (500.0, 500.0)})


class CoauthorPairsTest(unittest.TestCase):
    def test_small_publication_links_every_pair(self):
        pairs = set(coauthor_pairs(["A1", "E1", "E2"], frozenset({"E1", "E2"})))
        self.assertEqual(pairs, {("A1", "E1"), ("A1", "E2"), ("E1", "E2")})

    def test_large_collaboration_only_links_itmo_authors(self):
        externals = [f"E{i:03d}" for i in range(60)]
        pairs = set(coauthor_pairs(sorted(["A1", "A2", *externals]), frozenset(externals)))
        self.assertIn(("A1", "A2"), pairs)
        self.assertIn(("A1", "E000"), pairs)
        self.assertNotIn(("E000", "E001"), pairs)
        self.assertEqual(len(pairs), 1 + 2 * 60)


class Fa2LayoutTest(unittest.TestCase):
    @staticmethod
    def two_communities():
        import networkx as nx

        graph = nx.Graph()
        left, right = [f"l{i}" for i in range(8)], [f"r{i}" for i in range(8)]
        for group in (left, right):
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    graph.add_edge(a, b, weight=3.0)
        graph.add_edge("l0", "r0", weight=1.0)
        return graph, left, right

    def test_same_seed_gives_the_same_layout(self):
        graph, _, _ = self.two_communities()
        self.assertEqual(fa2_layout(graph, 100, seed=42), fa2_layout(graph, 100, seed=42))

    def test_does_not_touch_global_random_state(self):
        """The seed must not leak through the global random/numpy state -
        the next run of anything else stays unaffected."""
        import numpy as np

        graph, _, _ = self.two_communities()
        random.seed(0)
        np.random.seed(0)
        expected = (random.random(), np.random.random())
        random.seed(0)
        np.random.seed(0)
        fa2_layout(graph, 10, seed=42)
        self.assertEqual((random.random(), np.random.random()), expected)

    def test_connected_nodes_end_up_closer_than_unconnected(self):
        graph, left, right = self.two_communities()
        pos = fa2_layout(graph, 200, seed=1)

        def mean_dist(pairs):
            return sum(((pos[a][0] - pos[b][0]) ** 2 + (pos[a][1] - pos[b][1]) ** 2) ** 0.5 for a, b in pairs) / len(pairs)

        inside = [(a, b) for group in (left, right) for a in group for b in group if a < b]
        across = [(a, b) for a in left for b in right]
        self.assertLess(mean_dist(inside), mean_dist(across) / 2)


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
        """No giant component (no edges at all) - everything goes through
        singleton blending, must not raise."""
        pos, (n_giant, e_giant, n_small, n_single) = fa2_blended_layout({}, {"a", "b", "c"}, max_iter=10, seed=1)
        self.assertEqual(set(pos), {"a", "b", "c"})
        self.assertEqual((n_giant, e_giant, n_small), (0, 0, 0))
        self.assertEqual(n_single, 3)


class ForceAtlasLayouterTest(unittest.TestCase):
    """`ForceAtlasLayouter` is a thin wrapper around fa2_blended_layout/
    spread_min_distance/fit_coords with seed as state - checks that the
    wrapper actually forwards calls, not the math itself again (already
    covered by the tests above)."""

    def test_blended_positions_every_node_and_is_deterministic(self):
        layouter = ForceAtlasLayouter(seed=1)
        all_ids = {"a", "b", "c"}
        pos, stats = layouter.blended({("a", "b"): 2.0}, all_ids, max_iter=10, min_sep=1.0)
        self.assertEqual(set(pos), all_ids)
        self.assertEqual(len(stats), 4)


class RepoEdgeSignalsTest(unittest.TestCase):
    def test_small_shared_group_outweighs_a_large_one(self):
        # r1/r2 share one publication; r3..r7 are five repos of one lab account.
        pair_w = co_membership_weights([{"r1", "r2"}], 3.0)
        pair_w_owner = co_membership_weights([{"r3", "r4", "r5", "r6", "r7"}], 1.0)
        self.assertEqual(pair_w, {("r1", "r2"): 3.0})
        self.assertAlmostEqual(pair_w_owner[("r3", "r4")], 0.25)

    def test_groups_over_the_cap_and_singletons_give_no_edges(self):
        self.assertEqual(co_membership_weights([{"a"}, {"a", "b", "c"}], 1.0, cap=2), {})

    def test_groups_of_inverts_membership(self):
        groups = groups_of({"r1": ["p1"], "r2": ["p1", "p2"], "r3": []})
        self.assertEqual(sorted(map(sorted, groups)), [["r1", "r2"], ["r2"]])

    def test_top_k_keeps_each_nodes_strongest_edges(self):
        pair_w = {("a", "b"): 3.0, ("a", "c"): 2.0, ("a", "d"): 1.0}
        # a keeps b; c and d each keep their only edge (to a), so the union keeps all three.
        self.assertEqual(top_k_edges(pair_w, 1), pair_w)
        self.assertEqual(top_k_edges({("a", "b"): 3.0, ("a", "c"): 2.0, ("b", "c"): 1.0}, 1),
                         {("a", "b"): 3.0, ("a", "c"): 2.0})
