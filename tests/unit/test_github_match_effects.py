import unittest

from pauk.graph.extract import extract_node, person_spec
from pauk.models import Contribution, GitHubMatchResult, Person
from pauk.pipeline.github_matches import apply_matches, retract_matches, withdraw_matches
from pauk.pipeline.normalize import _merge_person


class GitHubMatchEffectsTest(unittest.TestCase):
    def setUp(self):
        self.person = Person(id="A1", is_itmo=True)
        self.contribution = Contribution(repository_id="R1", role="owner")
        self.match = GitHubMatchResult(login="coder42", email="account@lab.test",
                                       contributed_to=[self.contribution])

    def test_later_independent_changes_survive_retraction(self):
        apply_matches(self.person, [self.match])
        self.person.github = "other-login"
        self.person.email = "new@lab.test"
        independent = Contribution(repository_id="R2", role="contributor")
        self.person.contributed_to.append(independent)

        retract_matches(self.person)

        self.assertEqual(self.person.github, "other-login")
        self.assertEqual(self.person.email, "new@lab.test")
        self.assertEqual(self.person.contributed_to, [independent])

    def test_an_independently_confirmed_address_survives_retraction(self):
        apply_matches(self.person, [self.match])
        self.person.emails = ["ACCOUNT@LAB.TEST"]

        retract_matches(self.person)

        self.assertEqual(self.person.email, "account@lab.test")
        self.assertEqual(self.person.github_match.cleared_fields, ["github"])

    def test_withdrawals_survive_storage_and_repeated_empty_results(self):
        apply_matches(self.person, [self.match])
        retract_matches(self.person)
        apply_matches(self.person, [])
        self.person = Person.model_validate(self.person.model_dump(mode="json"))

        retract_matches(self.person)
        apply_matches(self.person, [])

        self.assertEqual(self.person.github_match.cleared_fields, ["email", "github"])

    def test_reapplying_the_match_cancels_its_pending_withdrawals(self):
        apply_matches(self.person, [self.match])
        retract_matches(self.person)

        apply_matches(self.person, [self.match])

        self.assertEqual(self.person.github_match.cleared_fields, [])
        self.assertEqual(self.person.github, "coder42")
        self.assertEqual(self.person.email, "account@lab.test")

    def test_merging_keeps_withdrawals_from_either_source(self):
        for removed_first in (False, True):
            with self.subTest(removed_first=removed_first):
                removed = Person(id="A1", is_itmo=True)
                apply_matches(removed, [self.match])
                retract_matches(removed)
                independent = Person(id="A2", is_itmo=True, email="source@lab.test")
                base, extra = (removed, independent) if removed_first else (independent, removed)

                _merge_person(base, extra)

                self.assertEqual(base.github_match.cleared_fields, ["github"])
                self.assertEqual(base.email, "source@lab.test")

    def test_a_shared_contribution_stays_while_another_account_supports_it(self):
        second = GitHubMatchResult(login="second", contributed_to=[self.contribution])
        apply_matches(self.person, [self.match, second])
        retract_matches(self.person)

        apply_matches(self.person, [second])

        self.assertEqual(self.person.contributed_to, [self.contribution])
        self.assertEqual(self.person.github, "second")
        self.assertIsNone(self.person.email)

    def test_withdrawing_one_account_keeps_another_accounts_shared_contribution(self):
        second = GitHubMatchResult(login="second", contributed_to=[self.contribution])
        apply_matches(self.person, [self.match, second])

        withdraw_matches(self.person, {"coder42"})

        self.assertEqual(self.person.github, "second")
        self.assertEqual(self.person.contributed_to, [self.contribution])
        self.assertEqual(self.person.github_match.cleared_contributions, [])
        self.assertEqual(self.person.github_match.withdrawn_logins, ["coder42"])

    def test_withdrawn_edges_survive_repeats_and_are_cancelled_by_a_new_source(self):
        apply_matches(self.person, [self.match])
        withdraw_matches(self.person, {"coder42"})
        retract_matches(self.person)
        apply_matches(self.person, [])
        self.assertEqual(self.person.github_match.cleared_contributions, [self.contribution])
        self.person.contributed_to.append(self.contribution.model_copy(deep=True))

        apply_matches(self.person, [])

        self.assertEqual(self.person.github_match.cleared_contributions, [])
        self.assertEqual(self.person.contributed_to, [self.contribution])

    def test_an_independent_source_on_either_merged_row_survives_retraction(self):
        for independent_first in (False, True):
            with self.subTest(independent_first=independent_first):
                derived = Person(id="A1", is_itmo=True)
                apply_matches(derived, [self.match])
                independent = Person(id="A2", is_itmo=True, github="coder42",
                                     email="account@lab.test", contributed_to=[self.contribution])
                base, extra = (independent, derived) if independent_first else (derived, independent)
                before_extra = extra.model_dump()

                _merge_person(base, extra)
                retract_matches(base)

                self.assertEqual(base.github, "coder42")
                self.assertEqual(base.email, "account@lab.test")
                self.assertEqual(base.contributed_to, [self.contribution])
                self.assertEqual(extra.model_dump(), before_extra)

    def test_a_merge_does_not_promote_a_copied_address_into_source_evidence(self):
        apply_matches(self.person, [self.match])

        _merge_person(self.person, Person(id="A2", is_itmo=True))

        self.assertEqual(self.person.emails, [])
        retract_matches(self.person)
        self.assertIsNone(self.person.email)

    def test_match_provenance_is_not_published_as_graph_properties(self):
        apply_matches(self.person, [self.match])

        row = self.person.model_dump(mode="json", by_alias=True)
        _, (_, properties) = extract_node(row, person_spec(row))

        self.assertNotIn("github_match", properties)
        self.assertEqual(properties["github"], "coder42")
