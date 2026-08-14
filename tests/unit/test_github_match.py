import json
import tempfile
import unittest
from pathlib import Path

import mongomock

from pauk.models import GitHubProfile, Person, Repository
from pauk.pipeline.stages.github_match import (
    MATCHES_FILENAME,
    GitHubMatchStage,
    confidence,
    decide,
    login_carries_surname,
    name_similarity,
    pick_email,
    score_account,
)
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


def person(pid, name, *, itmo=True, email=None, emails=(), variants=(), other=(),
           works=(), github=None):
    return Person(
        id=pid, openalex_id=pid, is_itmo=itmo, name_en=name, email=email, github=github,
        emails=list(emails), name_variants=list(variants), other_names=list(other),
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
        self.assertTrue(login_carries_surname("ipetrov", {"petrov"}))
        self.assertTrue(login_carries_surname("ivan-petrov", {"petrov"}))
        self.assertFalse(login_carries_surname("xyz123", {"petrov"}))

    def test_any_spelling_of_the_surname_counts(self):
        self.assertTrue(login_carries_surname("aduhanov", {"dukhanov", "duhanov"}))

    def test_an_author_without_a_usable_surname_matches_no_login(self):
        self.assertFalse(login_carries_surname("anything", set()))


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


class ScoreAccountTest(unittest.TestCase):
    """Every signal the matcher can raise, raised from the fields it reads.

    decide() is tested on signal lists written by hand. This covers the
    step before it: turning a harvested account and an author into that
    list. A signal that stopped being raised would leave decide() correct
    and the matcher blind.
    """

    @staticmethod
    def account(login="ipetrov", *, names=(), emails=(), itmo_text=False,
                org_itmo=False, is_owner=False):
        return {"login": login, "url": "", "names": set(names), "emails": set(emails),
                "itmo_text": itmo_text, "org_itmo": org_itmo, "is_owner": is_owner,
                "publication_ids": set(), "repos": set()}

    @staticmethod
    def author(*, names=(), emails=(), surnames=()):
        return {"names": set(names), "emails": set(emails), "surnames": set(surnames)}

    def signals(self, account, author, email_hit=False):
        return score_account(account, author, email_hit)[1]

    def test_a_shared_address_is_the_strongest_signal(self):
        score, signals, evidence = score_account(
            self.account(emails={"ivan@itmo.ru"}), self.author(emails={"ivan@itmo.ru"}),
            email_hit=True)
        self.assertIn("email_exact", signals)
        self.assertEqual(evidence["email"], ["ivan@itmo.ru"])
        self.assertEqual(score, 1.0)

    def test_the_same_name_on_both_sides(self):
        signals = self.signals(self.account(names={"ivan petrov"}),
                               self.author(names={"petrov ivan"}))
        self.assertIn("name_exact", signals)
        self.assertNotIn("name_fuzzy", signals)

    def test_a_name_that_is_close_but_not_the_same(self):
        # One letter apart: the transliterations an author publishes under.
        signals, evidence = score_account(
            self.account(names={"aleksandr dukhanov"}),
            self.author(names={"aleksander dukhanov"}), False)[1:]
        self.assertIn("name_fuzzy", signals)
        self.assertNotIn("name_exact", signals)
        self.assertGreaterEqual(evidence["name_similarity"], 0.86)

    def test_a_name_too_far_apart_raises_nothing(self):
        signals = self.signals(self.account(names={"ivan petrov"}),
                               self.author(names={"maria sidorova"}))
        self.assertEqual(signals, [])

    def test_itmo_anywhere_in_the_profile_text(self):
        # company, location and bio are read as one string, so any of the
        # three raises it.
        for field in ("company", "location", "bio"):
            with self.subTest(field=field):
                self.assertIn("itmo_profile",
                              self.signals(self.account(itmo_text=True), self.author()))

    def test_a_university_address_on_the_account(self):
        self.assertIn("itmo_email", self.signals(
            self.account(emails={"someone@itmo.ru"}), self.author()))

    def test_a_login_built_from_the_authors_surname(self):
        self.assertIn("login_surname", self.signals(
            self.account(login="apetrov"), self.author(surnames={"petrov"})))

    def test_owning_the_repository(self):
        self.assertIn("owner", self.signals(self.account(is_owner=True), self.author()))

    def test_a_repository_under_an_itmo_organization(self):
        self.assertIn("org_itmo", self.signals(self.account(org_itmo=True), self.author()))

    def test_weights_add_up_and_stop_at_one(self):
        score = score_account(
            self.account(login="petrov", names={"ivan petrov"}, emails={"p@itmo.ru"},
                         itmo_text=True, org_itmo=True, is_owner=True),
            self.author(names={"ivan petrov"}, surnames={"petrov"}), True)[0]
        self.assertEqual(score, 1.0)


class ConfidenceTest(unittest.TestCase):
    def test_evidence_about_this_person_makes_it_high(self):
        self.assertEqual(confidence(["email_exact"], in_bridge=False), "high")
        self.assertEqual(confidence(["name_exact", "owner"], in_bridge=False), "high")
        self.assertEqual(confidence(["name_exact"], in_bridge=True), "high")

    def test_a_name_plus_a_university_wide_signal_is_only_probable(self):
        # Nothing here belongs to this person alone: the name is shared and
        # ITMO in a profile is shared by thousands.
        self.assertEqual(confidence(["name_exact", "itmo_profile"], in_bridge=False), "probable")
        self.assertEqual(confidence(["name_exact", "org_itmo"], in_bridge=False), "probable")


class GitHubMatchStageTest(unittest.TestCase):
    def run_stage(self, people, profiles, repositories):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Settings(data_dir=Path(tmp.name))
        db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(db, "sample")
        raw = RawStore(db, "sample")
        self.prepared.write_models("persons", people)
        self.prepared.write_models("github_profiles", profiles)
        self.prepared.write_models("repositories", repositories)
        self.config = config
        result = GitHubMatchStage(self.prepared, raw, config).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def journal(self):
        path = self.config.audit_dir / self.prepared.group / MATCHES_FILENAME
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

    def test_an_account_is_recognised_by_a_second_address(self):
        # ORCID lists several addresses for a third of the people who list
        # any; the account committed with one that is not on the card.
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", email="shown@itmo.ru",
                    emails=["shown@itmo.ru", "old.address@gmail.com"])],
            [profile("someone", emails=["old.address@gmail.com"])],
            [],
        )
        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "someone")

    def test_a_spelling_registered_on_orcid_identifies_the_account(self):
        # OpenAlex knows the author as "R. Dangana"; the account is signed
        # with the name they registered on ORCID themselves.
        result, people = self.run_stage(
            [person("A1", "R. Dangana", other=["Reuben Samson Dangana"], works=["W1"])],
            [profile("rsd", name="Reuben Samson Dangana",
                     repos=["https://github.com/rsd/tool"])],
            [repository("rsd", "tool", works=["W1"])],
        )
        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "rsd")

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

    def test_a_match_records_the_repositories_as_the_persons_work(self):
        _result, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"])],
            [profile("ipetrov", name="Ivan Petrov",
                     repos=["https://github.com/ipetrov/own", "https://github.com/lab/shared"])],
            [repository("ipetrov", "own", works=["W1"]), repository("lab", "shared", works=["W1"])],
        )
        roles = {c.repository_id: c.role for c in people["A1"].contributed_to}
        self.assertEqual(roles, {"github_ipetrov_own": "owner",
                                 "github_lab_shared": "contributor"})

    def test_the_journal_records_how_much_the_match_rests_on(self):
        self.run_stage(
            [person("A1", "Ivan Petrov", email="ivan@itmo.ru")],
            [profile("someone", emails=["ivan@itmo.ru"])],
            [],
        )
        self.assertEqual(self.journal()[0]["confidence"], "high")

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