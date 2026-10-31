import unittest

from pauk.graph.extract import extract_node, person_spec
from pauk.models import Contribution, GitHubMatchResult, Person
from pauk.pipeline.github_matches import apply_matches, retract_matches
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

    def test_a_shared_contribution_stays_while_another_account_supports_it(self):
        second = GitHubMatchResult(login="second", contributed_to=[self.contribution])
        apply_matches(self.person, [self.match, second])
        retract_matches(self.person)

        apply_matches(self.person, [second])

        self.assertEqual(self.person.contributed_to, [self.contribution])
        self.assertEqual(self.person.github, "second")
        self.assertIsNone(self.person.email)

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
