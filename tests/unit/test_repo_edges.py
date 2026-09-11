import unittest

from pauk.gui.generate_data import repo_cluster_keys
from pauk.gui.layout import co_membership_weights, majority_vote, top_k_edges
from pauk.pipeline.stages.repositories import _payload_date, _url_repo_id


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


class RepoClusterKeysTest(unittest.TestCase):
    """Department, else owning organization, else the field of its papers."""

    def keys(self, repo_ids, *, dept=None, org=None, field=None, min_size=2):
        return repo_cluster_keys(repo_ids, dept or {}, org or {}, field or {}, min_size)

    def test_a_department_wins_over_everything_else(self):
        got = self.keys(["r"], dept={"r": "d1"}, org={"r": "acme"}, field={"r": "CS"})
        self.assertEqual(got, {"r": ("dept", "d1")})

    def test_a_department_of_one_still_counts(self):
        # Exempt from the threshold: it exists outside this map, and keeps the
        # colour it has on the authors' and publications' tabs.
        self.assertEqual(self.keys(["r"], dept={"r": "d1"}), {"r": ("dept", "d1")})

    def test_an_organization_needs_more_than_one_repository(self):
        one = self.keys(["a"], org={"a": "acme"}, field={"a": "CS"})
        self.assertEqual(one, {"a": None})  # "CS" is a field of one as well

        two = self.keys(["a", "b"], org={"a": "acme", "b": "acme"})
        self.assertEqual(two, {"a": ("org", "acme"), "b": ("org", "acme")})

    def test_a_repository_an_organization_turns_down_falls_through_to_its_field(self):
        got = self.keys(
            ["a", "b", "c"],
            org={"a": "solo"},
            field={"a": "CS", "b": "CS", "c": "CS"},
        )
        self.assertEqual(got["a"], ("field", "CS"))

    def test_an_organizations_size_counts_every_repository_it_owns(self):
        # `b` has a department, but it still makes acme an organization of two,
        # which is what lets `a` cluster by it.
        got = self.keys(
            ["a", "b"],
            dept={"b": "d1"},
            org={"a": "acme", "b": "acme"},
        )
        self.assertEqual(got, {"a": ("org", "acme"), "b": ("dept", "d1")})

    def test_a_field_of_one_is_dropped_rather_than_kept(self):
        got = self.keys(["a", "b", "c"], field={"a": "CS", "b": "CS", "c": "Medicine"})
        self.assertEqual(got["a"], ("field", "CS"))
        self.assertIsNone(got["c"])

    def test_a_repository_with_nothing_to_go_on(self):
        self.assertEqual(self.keys(["r"]), {"r": None})

    def test_every_repository_gets_an_entry(self):
        got = self.keys(["a", "b"], dept={"a": "d1"})
        self.assertEqual(sorted(got), ["a", "b"])


if __name__ == "__main__":
    unittest.main()


class MajorityVoteTest(unittest.TestCase):
    """One vote for departments and publication fields alike.

    Both are read off rows Neo4j returns in no defined order, so a tie has to
    be settled by the value itself — otherwise the cluster a repository lands
    in, and the colour it is drawn with, move between runs over one dataset.
    """

    def test_the_most_common_value_wins(self):
        self.assertEqual(majority_vote([["ml"], ["ml"], ["bio"]]), "ml")

    def test_a_tie_is_broken_by_name_not_by_input_order(self):
        forward = majority_vote([["bio"], ["ml"]])
        backward = majority_vote([["ml"], ["bio"]])
        self.assertEqual(forward, "bio")
        self.assertEqual(forward, backward)

    def test_nothing_to_count_is_no_answer(self):
        self.assertIsNone(majority_vote([]))
        self.assertIsNone(majority_vote([[], []]))


class UrlRepoIdTest(unittest.TestCase):
    """The key both passes of RepositoriesStage claim their work by."""

    def test_owner_and_name_are_lowercased(self):
        self.assertEqual(_url_repo_id("https://github.com/Org/Repo"), "github_org_repo")

    def test_a_trailing_slash_does_not_change_the_key(self):
        self.assertEqual(_url_repo_id("https://github.com/org/repo/"),
                         _url_repo_id("https://github.com/org/repo"))

    def test_anything_that_is_not_a_repository_url_has_no_key(self):
        self.assertIsNone(_url_repo_id("https://gitlab.com/org/repo"))
        self.assertIsNone(_url_repo_id("https://github.com/org/repo/tree/main"))
        self.assertIsNone(_url_repo_id(None))
