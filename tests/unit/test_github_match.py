import json
import tempfile
import unittest
from pathlib import Path

from pauk.models import GitHubProfile, Person, Repository
from pauk.pipeline.stages.github_match import (
    MATCHES_FILENAME,
    GitHubMatchStage,
    decide,
    login_carries_surname,
    name_similarity,
    pick_email,
)
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


def person(pid, name, *, itmo=True, email=None, variants=(), works=(), github=None):
    return Person(
        id=pid, openalex_id=pid, is_itmo=itmo, name_en=name, email=email, github=github,
        name_variants=list(variants),
        authored=[{"publication_id": work, "position": 1} for work in works],
    )


def profile(login, *, name=None, emails=(), commit_names=(), repos=(),
            company=None, location=None, bio=None, account_type="user"):
    return GitHubProfile(
        id=f"github_{login.lower()}", login=login, name=name, type=account_type,
        emails=list(emails), commit_names=list(commit_names), repos=list(repos),
        company=company, location=location, description=bio,
    )


def repository(owner, name, *, works=()):
    url = f"https://github.com/{owner}/{name}"
    return Repository(id=f"github_{owner.lower()}_{name.lower()}", name=name, url=url,
                      owner_login=owner, publication_ids=list(works), cited_urls=[url])


class NameSimilarityTest(unittest.TestCase):
    def test_word_order_does_not_change_a_name(self):
        self.assertEqual(name_similarity("ivan petrov", "petrov ivan"), 1.0)

    def test_one_word_is_never_an_exact_match(self):
        # Half the pool shares a surname; matching on it alone would fire
        # on every Petrov at the university.
        self.assertLess(name_similarity("petrov", "petrov ivan"), 1.0)

    def test_an_initial_does_not_reach_the_fuzzy_threshold(self):
        self.assertLess(name_similarity("ivan petrov", "i petrov"), 0.86)

    def test_login_built_from_the_surname(self):
        self.assertTrue(login_carries_surname("ipetrov", "petrov"))
        self.assertTrue(login_carries_surname("ivan-petrov", "petrov"))
        self.assertFalse(login_carries_surname("xyz123", "petrov"))

    def test_a_short_surname_is_never_matched_against_a_login(self):
        # Guarded at the caller: an author whose last word is an initial
        # gets surname=None, and this returns False for it.
        self.assertFalse(login_carries_surname("anything", None))


class PickEmailTest(unittest.TestCase):
    def test_a_university_address_wins(self):
        self.assertEqual(
            pick_email({"personal@gmail.com", "ivan@itmo.ru"}), "ivan@itmo.ru")

    def test_noreply_is_not_an_address_for_a_person(self):
        self.assertIsNone(pick_email({"1234+bob@users.noreply.github.com"}))

    def test_nothing_usable_gives_nothing(self):
        self.assertIsNone(pick_email(set()))
        self.assertIsNone(pick_email({"not-an-address"}))

    def test_the_choice_does_not_depend_on_set_order(self):
        options = {"a.very.long.alias@gmail.com", "ip@mail.ru"}
        self.assertEqual(pick_email(options), pick_email(set(reversed(list(options)))))


class DecideTest(unittest.TestCase):
    def test_an_email_settles_it(self):
        self.assertEqual(decide(["email_exact"], in_bridge=False), "matched")

    def test_an_exact_name_needs_the_bridge_or_corroboration(self):
        self.assertEqual(decide(["name_exact"], in_bridge=True), "matched")
        self.assertEqual(decide(["name_exact", "owner"], in_bridge=False), "matched")
        self.assertEqual(decide(["name_exact"], in_bridge=False), "review")

    def test_a_fuzzy_name_needs_both(self):
        self.assertEqual(decide(["name_fuzzy", "owner"], in_bridge=True), "matched")
        self.assertEqual(decide(["name_fuzzy"], in_bridge=True), "review")
        self.assertEqual(decide(["name_fuzzy"], in_bridge=False), "rejected")

    def test_corroboration_alone_identifies_nobody(self):
        self.assertEqual(decide(["owner", "itmo_profile", "org_itmo"], in_bridge=True), "rejected")


class GitHubMatchStageTest(unittest.TestCase):
    def run_stage(self, people, profiles, repositories):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Settings(data_dir=Path(tmp.name))
        self.prepared = PreparedStore(config.prepared_dir, "sample")
        raw = RawStore(config.raw_dir, "sample")
        self.prepared.write_models("persons", people)
        self.prepared.write_models("github_profiles", profiles)
        self.prepared.write_models("repositories", repositories)
        result = GitHubMatchStage(self.prepared, raw, config).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def journal(self):
        path = self.prepared.group_dir / MATCHES_FILENAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_a_shared_email_matches_without_anything_else(self):
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", email="ivan@itmo.ru")],
            [profile("someone", emails=["ivan@itmo.ru"])],
            [],
        )
        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "someone")
        (row,) = self.journal()
        self.assertIn("email_exact", row["signals"])

    def test_a_name_on_a_cited_repository_matches(self):
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"])],
            [profile("ipetrov", name="Ivan Petrov", repos=["https://github.com/ipetrov/tool"])],
            [repository("ipetrov", "tool", works=["W1"])],
        )
        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "ipetrov")
        (row,) = self.journal()
        self.assertTrue(row["evidence"]["in_bridge"])

    def test_a_name_with_no_connection_waits_for_a_human(self):
        # The account never appears on a repository this author's papers
        # cite, and nothing else backs the name.
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"])],
            [profile("stranger", name="Ivan Petrov")],
            [],
        )
        self.assertEqual((result["github_matched"], result["github_review"]), (0, 1))
        self.assertIsNone(people["A1"].github)
        self.assertEqual(self.journal()[0]["decision"], "review")

    def test_two_authors_fitting_equally_well_are_not_guessed_between(self):
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"]), person("A2", "Ivan Petrov", works=["W1"])],
            [profile("ipetrov", name="Ivan Petrov", repos=["https://github.com/ipetrov/tool"])],
            [repository("ipetrov", "tool", works=["W1"])],
        )
        self.assertEqual((result["github_matched"], result["github_review"]), (0, 1))
        self.assertTrue(self.journal()[0]["evidence"]["ambiguous"])
        self.assertIsNone(people["A1"].github)

    def test_an_external_author_is_never_matched(self):
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", itmo=False, email="ivan@itmo.ru")],
            [profile("ipetrov", emails=["ivan@itmo.ru"])],
            [],
        )
        self.assertEqual(result["github_matched"], 0)
        self.assertIsNone(people["A1"].github)

    def test_an_organization_account_is_not_a_person(self):
        result, _people = self.run_stage(
            [person("A1", "Ivan Petrov", email="ivan@itmo.ru")],
            [profile("itmo-team", emails=["ivan@itmo.ru"], account_type="organization")],
            [],
        )
        self.assertEqual(result["github_accounts"], 0)
        self.assertEqual(result["github_matched"], 0)

    def test_a_matched_account_gives_the_author_an_address(self):
        # The account commits with an address the author never published.
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"])],
            [profile("ipetrov", name="Ivan Petrov", emails=["ivan@itmo.ru"],
                     repos=["https://github.com/ipetrov/tool"])],
            [repository("ipetrov", "tool", works=["W1"])],
        )
        self.assertEqual(result["github_emails"], 1)
        self.assertEqual(people["A1"].email, "ivan@itmo.ru")

    def test_an_address_the_author_published_is_not_replaced(self):
        _result, people = self.run_stage(
            [person("A1", "Ivan Petrov", email="published@orcid.org", works=["W1"])],
            [profile("ipetrov", name="Ivan Petrov", emails=["ivan@itmo.ru"],
                     repos=["https://github.com/ipetrov/tool"])],
            [repository("ipetrov", "tool", works=["W1"])],
        )
        self.assertEqual(people["A1"].email, "published@orcid.org")

    def test_an_existing_login_is_not_overwritten(self):
        _result, people = self.run_stage(
            [person("A1", "Ivan Petrov", email="ivan@itmo.ru", github="chosen-by-hand")],
            [profile("someone", emails=["ivan@itmo.ru"])],
            [],
        )
        self.assertEqual(people["A1"].github, "chosen-by-hand")

    def test_ritmo_is_not_itmo(self):
        # "RITMO, University of Oslo" contains the same four letters and
        # must not corroborate anything.
        result, _people = self.run_stage(
            [person("A1", "Ivan Petrov")],
            [profile("finn", name="Ivan Petrov", company="RITMO, University of Oslo")],
            [],
        )
        (row,) = self.journal()
        self.assertNotIn("itmo_profile", row["signals"])
        self.assertEqual(result["github_matched"], 0)

    def test_a_login_built_from_the_surname_corroborates_the_name(self):
        # "msidorova" is not evidence on its own, but it is not a
        # coincidence either: it carries the author's surname.
        result, people = self.run_stage(
            [person("A1", "Maria Sidorova")],
            [profile("msidorova", name="Maria Sidorova")],
            [],
        )
        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "msidorova")
        self.assertIn("login_surname", self.journal()[0]["signals"])

    def test_the_journal_records_every_decision(self):
        self.run_stage(
            [person("A1", "Ivan Petrov", email="ivan@itmo.ru"),
             person("A2", "Maria Sidorova")],
            [profile("ipetrov", emails=["ivan@itmo.ru"]),
             profile("coder42", name="Maria Sidorova")],
            [],
        )
        rows = {row["login"]: row for row in self.journal()}
        self.assertEqual(rows["ipetrov"]["decision"], "matched")
        self.assertEqual(rows["ipetrov"]["person"], "A1")
        # Nothing but the name behind this one, and the login says nothing.
        self.assertEqual(rows["coder42"]["decision"], "review")


if __name__ == "__main__":
    unittest.main()