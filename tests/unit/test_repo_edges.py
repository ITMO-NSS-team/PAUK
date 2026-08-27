import unittest

from pauk.gui.layout import co_membership_weights, top_k_edges
from pauk.pipeline.stages.repositories import _payload_date


class CoMembershipWeightsTest(unittest.TestCase):
    def test_pair_sharing_one_group_gets_the_full_weight(self):
        self.assertEqual(co_membership_weights([["a", "b"]], 3.0), {("a", "b"): 3.0})

    def test_a_group_of_one_connects_nothing(self):
        self.assertEqual(co_membership_weights([["a"], []], 3.0), {})

    def test_larger_group_splits_the_weight_instead_of_multiplying_pairs(self):
        # Four members are six pairs, but each member's total pull stays at
        # `weight` — otherwise one lab account outweighs every real signal.
        weights = co_membership_weights([["a", "b", "c", "d"]], 3.0)
        self.assertEqual(len(weights), 6)
        for value in weights.values():
            self.assertAlmostEqual(value, 1.0)
        pull_of_a = sum(w for pair, w in weights.items() if "a" in pair)
        self.assertAlmostEqual(pull_of_a, 3.0)

    def test_pairs_are_ordered_so_the_same_edge_never_appears_twice(self):
        weights = co_membership_weights([["b", "a"], ["a", "b"]], 1.0)
        self.assertEqual(list(weights), [("a", "b")])
        self.assertAlmostEqual(weights[("a", "b")], 2.0)

    def test_duplicate_members_of_one_group_count_once(self):
        self.assertEqual(co_membership_weights([["a", "b", "a"]], 2.0), {("a", "b"): 2.0})

    def test_group_over_the_cap_is_dropped_entirely(self):
        self.assertEqual(co_membership_weights([["a", "b", "c"]], 1.0, cap=2), {})
        self.assertEqual(len(co_membership_weights([["a", "b", "c"]], 1.0, cap=3)), 3)


class TopKEdgesTest(unittest.TestCase):
    def test_drops_an_edge_both_of_whose_ends_have_stronger_ones(self):
        # Every node here has two edges worth more than a-d, so a-d is the one
        # nobody keeps.
        pair_w = {
            ("a", "b"): 3.0, ("a", "c"): 3.0,
            ("d", "b"): 2.0, ("d", "c"): 2.0,
            ("a", "d"): 1.0,
        }
        self.assertNotIn(("a", "d"), top_k_edges(pair_w, 2))
        self.assertEqual(len(top_k_edges(pair_w, 2)), 4)

    def test_weak_edge_survives_when_it_is_its_other_end_s_only_one(self):
        # The union of per-node top-K, not a global cut: d would otherwise be
        # stranded even though a-d is the only edge it has.
        pair_w = {("a", "b"): 3.0, ("a", "c"): 2.0, ("a", "d"): 1.0}
        kept = top_k_edges(pair_w, 1)
        self.assertEqual(kept[("a", "d")], 1.0)
        self.assertEqual(kept[("a", "b")], 3.0)

    def test_ties_break_on_id_so_the_same_input_gives_the_same_map(self):
        pair_w = {("a", "c"): 1.0, ("a", "b"): 1.0}
        self.assertEqual(top_k_edges(pair_w, 1), top_k_edges(dict(reversed(pair_w.items())), 1))
        self.assertIn(("a", "b"), top_k_edges(pair_w, 1))

    def test_empty_input(self):
        self.assertEqual(top_k_edges({}, 5), {})


class PayloadDateTest(unittest.TestCase):
    def test_github_timestamp_becomes_a_date(self):
        self.assertEqual(str(_payload_date("2024-05-17T09:31:02Z")), "2024-05-17")

    def test_missing_or_unparsable_value_is_none(self):
        self.assertIsNone(_payload_date(None))
        self.assertIsNone(_payload_date(""))
        self.assertIsNone(_payload_date("last tuesday"))


if __name__ == "__main__":
    unittest.main()
