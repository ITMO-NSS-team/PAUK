"""Unit tests for edges.py."""

from __future__ import annotations

import unittest

from new_generate.authorship import Authorship
from new_generate.departments import DepartmentAssignment, DepartmentTable
from new_generate.edges import EdgeBuilder
from new_generate.layout import Layout


def _layout(**overrides) -> Layout:
    base = {"pos_authors": {}, "pos_pubs": {}, "pos_repos": {}, "coauth": {}, "pub_pair_w": {}, "repo_edge_w": {}}
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
        """Regression test: dept_pair_w used to accumulate by iterating
        authorship.pub_ids (a set) directly - insertion order, and so the
        final dept_edges list order, then depended on per-process string
        hash randomization. Two live runs against the same real snapshot
        with the same seed produced byte-different graph-data.json files -
        identical content as sets, only dept_edges' list order differed.
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

        # P1 connects d1-d2, P2 connects d3-d4, P3 connects d5-d6 - processed
        # in sorted pid order (P1, P2, P3), dept_edges must list them in that
        # same order regardless of how pub_ids itself happens to iterate.
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
        layout = _layout(repo_edge_w={("r1", "r2"): 1})

        edges = EdgeBuilder(db, authorship, _assignment(), table, layout).build()

        self.assertEqual(edges["repo_edges"], [{"s": "r1", "t": "r2", "w": 1}])


if __name__ == "__main__":
    unittest.main()
