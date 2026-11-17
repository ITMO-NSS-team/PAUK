"""Unit tests for edges.py."""

from __future__ import annotations

import unittest

from pauk.gui.graph_builder.authorship import Authorship
from pauk.gui.graph_builder.departments import DepartmentAssignment, DepartmentTable
from pauk.gui.graph_builder.edges import EdgeBuilder
from pauk.gui.graph_builder.layout import Layout


def _layout(**overrides) -> Layout:
    base = {"pos_authors": {}, "pos_pubs": {}, "pos_repos": {}, "coauth": {}, "pub_pair_w": {}, "repo_edge_w": {}, "repo_edge_via": {}}
    base.update(overrides)
    return Layout(**base)


def _assignment(**overrides) -> DepartmentAssignment:
    base = {
        "static_depts": {},
        "pub_dept_rows": {},
        "pub_primary": {},
        "author_dept": {},
        "repo_pub_map": {},
        "repo_dept_rows": {},
        "repo_dept": {},
    }
    base.update(overrides)
    return DepartmentAssignment(**base)


class DeptEdgesOrderTest(unittest.TestCase):
    def test_order_follows_sorted_pub_ids_not_set_iteration_order(self):
        """dept_pair_w must accumulate over sorted pub_ids, not set iteration order.

        Otherwise string hash randomization made dept_edges list order (and so
        graph-data.json bytes) differ between runs on the same snapshot and seed.
        """
        db = {"repositories": [], "repo_persons": [], "repo_pubs": [], "authorship": []}
        authorship = Authorship(
            pub_authors={"P1": ["A1", "A2"], "P2": ["A3", "A4"], "P3": ["A5", "A6"]},
            author_pubs={},
            pubs_rows=[],
            pub_ids={"P3", "P1", "P2"},
        )
        assignment = _assignment(
            author_dept={"A1": "d1", "A2": "d2", "A3": "d3", "A4": "d4", "A5": "d5", "A6": "d6"}
        )
        gid = {"d1": 1, "d2": 2, "d3": 3, "d4": 4, "d5": 5, "d6": 6}
        table = DepartmentTable(departments=[], g=lambda d: gid[d] if d else 0, no_dept_gid=0)

        edges = EdgeBuilder(db, authorship, assignment, table, _layout()).build()

        # P1 connects d1-d2, P2 connects d3-d4, P3 connects d5-d6; dept_edges must
        # list them in sorted pid order regardless of pub_ids iteration order.
        self.assertEqual([(e["s"], e["t"]) for e in edges["dept_edges"]], [(1, 2), (3, 4), (5, 6)])


class WeightThresholdTest(unittest.TestCase):
    def test_coauth_edges_drop_pairs_below_the_minimum_weight(self):
        db = {"repositories": [], "repo_persons": [], "repo_pubs": [], "authorship": []}
        authorship = Authorship(pub_authors={}, author_pubs={}, pubs_rows=[], pub_ids=set())
        table = DepartmentTable(departments=[], g=lambda _d: 0, no_dept_gid=0)
        layout = _layout(coauth={("a1", "a2"): 1, ("a1", "a3"): 2})

        edges = EdgeBuilder(db, authorship, _assignment(), table, layout).build()

        self.assertEqual(edges["coauth_edges"], [{"s": "a1", "t": "a3", "w": 2}])

    def test_repo_edges_have_no_weight_threshold(self):
        db = {"repositories": [], "repo_persons": [], "repo_pubs": [], "authorship": []}
        authorship = Authorship(pub_authors={}, author_pubs={}, pubs_rows=[], pub_ids=set())
        table = DepartmentTable(departments=[], g=lambda _d: 0, no_dept_gid=0)
        layout = _layout(repo_edge_w={("r1", "r2"): 1 / 3}, repo_edge_via={("r1", "r2"): ["owner"]})

        edges = EdgeBuilder(db, authorship, _assignment(), table, layout).build()

        self.assertEqual(edges["repo_edges"], [{"s": "r1", "t": "r2", "w": 0.33, "via": ["owner"]}])


class VersionEdgesTest(unittest.TestCase):
    @staticmethod
    def _edges(rows=None, pub_ids=None):
        pub_ids = {"P1", "P2", "P3"} if pub_ids is None else pub_ids
        db = {"repositories": [], "repo_persons": [], "repo_pubs": [], "authorship": []}
        if rows is not None:
            db["pub_versions"] = rows
        authorship = Authorship(
            pub_authors={pid: [] for pid in pub_ids}, author_pubs={}, pubs_rows=[], pub_ids=pub_ids
        )
        table = DepartmentTable(departments=[], g=lambda _d: 0, no_dept_gid=0)
        return EdgeBuilder(db, authorship, _assignment(), table, _layout()).build()["version_edges"]

    def test_only_confirmed_status_is_accepted(self):
        for status in ("held", "different", "SAME", "", None, True):
            with self.subTest(status=status):
                self.assertEqual(self._edges([
                    {"version_pid": "P1", "pid": "P2", "status": status,
                     "mergeable": True, "llm_verdict": True, "bucket": "MEDIUM_FUZZY_LLM_SAME"}
                ]), [])
        self.assertEqual(self._edges([{"version_pid": "P1", "pid": "P2", "llm_verdict": True}]), [])
        for source in ("auto", "review", None):
            with self.subTest(confirmation_source=source):
                self.assertEqual(self._edges([{
                    "version_pid": "P2", "pid": "P1", "status": "same", "confirmation_source": source,
                }]), [{"s": "P2", "t": "P1"}])

    def test_missing_table_is_compatible_with_old_snapshots(self):
        self.assertEqual(self._edges(), [])

    def test_both_publications_must_be_on_the_map_and_self_loops_are_dropped(self):
        rows = [
            {"version_pid": source, "pid": target, "status": "same"}
            for source, target in (("P1", "P2"), ("P1", "P3"), ("P3", "P2"), ("P1", "P1"), (None, "P2"))
        ]
        self.assertEqual(self._edges(rows, {"P1", "P2"}), [{"s": "P1", "t": "P2"}])

    def test_pairs_are_sorted_deduplicated_and_keep_their_direction(self):
        rows = [
            {"version_pid": source, "pid": target, "status": "same", "reviewer": "private", "llm_reason": "private"}
            for source, target in (("P2", "P3"), ("P2", "P1"), ("P1", "P2"), ("P2", "P3"))
        ]
        expected = [{"s": "P1", "t": "P2"}, {"s": "P2", "t": "P1"}, {"s": "P2", "t": "P3"}]
        self.assertEqual(self._edges(rows), expected)
        self.assertEqual(self._edges(list(reversed(rows))), expected)


if __name__ == "__main__":
    unittest.main()
