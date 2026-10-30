import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import mongomock

from pauk.models import Contribution, GitHubProfile, Person, Repository
from pauk.pipeline.normalize import _merge_person
from pauk.pipeline.stages import social_graph
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.github_match import (
    ITMO_IN_TEXT,
    MATCHES_FILENAME,
    PETERSBURG_PATTERN,
    GitHubMatchStage,
    confidence,
    decide,
    login_carries_surname,
    name_similarity,
    pick_email,
    score_account,
)
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore, review


def person(pid, name, *, itmo=True, email=None, emails=(), variants=(), other=(),
           works=(), github=None):
    return Person(
        id=pid, openalex_id=pid, is_itmo=itmo, name_raw=name, email=email, github=github,
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

    def test_an_unbacked_spelling_links_with_support_and_asks_nobody_alone(self):
        self.assertEqual(decide(["name_alternative"], in_bridge=True), "matched")
        self.assertEqual(decide(["name_alternative", "itmo_profile"], in_bridge=False), "matched")
        self.assertEqual(decide(["name_alternative"], in_bridge=False), "rejected")


class ScoreAccountTest(unittest.TestCase):
    """Every signal the matcher can raise, raised from the fields it reads.

    decide() is tested on hand-written signal lists; this covers turning a
    harvested account and an author into that list.
    """

    @staticmethod
    def account(login="ipetrov", *, names=(), emails=(), itmo_text=False,
                org_itmo=False, is_owner=False):
        return {"login": login, "url": "", "names": set(names), "emails": set(emails),
                "itmo_text": itmo_text, "org_itmo": org_itmo, "is_owner": is_owner,
                "publication_ids": set(), "repos": set()}

    @staticmethod
    def author(*, names=(), emails=(), surnames=(), trusted=None):
        return {"names": set(names), "emails": set(emails), "surnames": set(surnames),
                "trusted": set(names if trusted is None else trusted)}

    def signals(self, account, author, email_hit=False):
        return score_account(account, author, email_hit)[1]

    def test_a_spelling_the_main_name_does_not_back_is_marked_apart(self):
        signals = self.signals(self.account(names={"xin zhang"}),
                               self.author(names={"xi zhang", "xin zhang"}, trusted={"xi zhang"}))
        self.assertIn("name_alternative", signals)
        self.assertNotIn("name_exact", signals)

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
    def test_organization_login_requires_itmo_identity_to_corroborate_a_match(self):
        for owner, matched in (("saint-petersburg-ai", False), ("ITMO-NCCR", True)):
            with self.subTest(owner=owner):
                repo = repository(owner, "tool")
                _, people = self.run_stage(
                    [person("A1", "Ivan Petrov")],
                    [profile("someone", name="Ivan Petrov", repos=[repo.url])],
                    [repo],
                )
                self.assertEqual(people["A1"].github, "someone" if matched else None)
                entry = self.journal()[0]
                self.assertEqual("org_itmo" in entry["signals"], matched)

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

    def test_a_shared_address_keeps_every_author_regardless_of_insertion_order(self):
        authors = [person("A1", "Ivan Petrov", email="shared@itmo.ru"),
                   person("A2", "Boris Smirnov", email="shared@itmo.ru")]
        for ordered in (authors, list(reversed(authors))):
            with self.subTest(order=[author.id for author in ordered]):
                result, people = self.run_stage(
                    ordered, [profile("coder42", emails=["shared@itmo.ru"])], [])

                self.assertEqual(result["github_matched"], 0)
                self.assertEqual(result["github_review"], 1)
                self.assertTrue(all(author.github is None for author in people.values()))
                (question,) = review.questions(self.prepared.db, answered=False)
                self.assertEqual(question["members"], ["A1", "A2", "coder42"])

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

    def test_a_tie_goes_to_the_author_whose_paper_cites_the_repository(self):
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"]), person("A2", "Ivan Petrov")],
            [profile("ipetrov", name="Ivan Petrov", company="ITMO University",
                     repos=["https://github.com/lab/tool"])],
            [repository("lab", "tool", works=["W1"])],
        )
        self.assertEqual((result["github_matched"], result["github_review"]), (1, 0))
        self.assertEqual(people["A1"].github, "ipetrov")
        self.assertIsNone(people["A2"].github)

    def test_a_shared_address_settles_a_tie_before_the_paper_does(self):
        _, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"]),
             person("A2", "Ivan Petrov", email="ivan@example.org")],
            [profile("ipetrov", name="Ivan Petrov", emails=["ivan@example.org"],
                     company="ITMO University", repos=["https://github.com/lab/tool"])],
            [repository("lab", "tool", works=["W1"])],
        )
        self.assertEqual(people["A2"].github, "ipetrov")
        self.assertIsNone(people["A1"].github)

    def test_a_tie_names_the_same_author_on_every_run(self):
        # A set iterates in a different order in every process.
        self.run_stage(
            [person(pid, "Ivan Petrov", works=["W1"]) for pid in ("A4", "A2", "A3", "A1")],
            [profile("ipetrov", name="Ivan Petrov", repos=["https://github.com/ipetrov/tool"])],
            [repository("ipetrov", "tool", works=["W1"])],
        )
        self.assertEqual(self.journal()[0]["person"], "A1")

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


class ItmoInTextTest(unittest.TestCase):
    """Person matching keeps the city signal; organization seeding does not."""

    def test_social_graph_uses_the_stricter_identity_pattern(self):
        self.assertIsNot(ITMO_IN_TEXT, social_graph.ITMO_IDENTITY_PATTERN)
        self.assertFalse(social_graph.ITMO_IDENTITY_PATTERN.search("Saint Petersburg"))

    def test_the_spellings_social_graph_added_are_recognised_here(self):
        for text in ("Россия, Санкт Петербург", "Санкт-Петербург",
                     "Russia, St. Petersburg", "St-Petersburg", "Sankt Petersburg",
                     "St.Petersburg", "Sankt-Peterburg", "СанктПетербург",
                     "Saint-Petersburg", "SaintPetersburg", "St Petersburg", "StPetersburg",
                     "Sankt Peterburg", "Saint Peterburg", "SAINT PETERSBURG",
                     "saint petersburg", "САНКТ-ПЕТЕРБУРГ"):
            with self.subTest(text=text):
                self.assertTrue(ITMO_IN_TEXT.search(text))

    def test_russian_case_endings_are_still_the_city(self):
        for text in ("в Санкт-Петербурге", "из Санкт-Петербурга", "по Санкт-Петербургу",
                     "о Санкт-Петербурге", "Санкт-Петербурге", "г. Санкт-Петербург",
                     "Санкт-Петербургский политехнический университет",
                     "Лаборатория, Санкт-Петербурга, Россия"):
            with self.subTest(text=text):
                self.assertTrue(ITMO_IN_TEXT.search(text))

    def test_bare_sankt_is_not_a_city_signal(self):
        self.assertFalse(PETERSBURG_PATTERN.search("Sankt Gallenkirch"))

    def test_itmo_as_a_word_still_wins_and_ritmo_still_does_not(self):
        self.assertTrue(ITMO_IN_TEXT.search("ITMO University"))
        self.assertFalse(ITMO_IN_TEXT.search("RITMO, University of Oslo"))


class GitHubReviewTest(unittest.TestCase):
    """Accounts the signals cannot settle, and what a person decides about them.

    An answer has to reach the rules on the next run, both ways round:
    applying a match they would only have shown, and stopping one they would
    have made.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Settings(data_dir=Path(tmp.name))
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")

    def run_stage(self, people, profiles, repositories=(), selection=None):
        self.prepared.write_models("persons", people)
        self.prepared.write_models("github_profiles", profiles)
        self.prepared.write_models("repositories", repositories)
        return self.rerun_stage(selection)

    def rerun_stage(self, selection=None):
        """Keep the rows saved by the previous run, as a real repeat does."""
        result = GitHubMatchStage(self.prepared, self.raw, self.config, selection=selection).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def unsure(self):
        """A name that matches exactly and nothing else backing it up."""
        return ([person("A1", "Stanislav Shtuka")],
                [profile("XieN-N", name="Stanislav Shtuka")])

    def test_an_account_nobody_can_place_becomes_a_question(self):
        result, people = self.run_stage(*self.unsure())
        self.assertEqual(result["github_review"], 1)
        self.assertIsNone(people["A1"].github)
        (question,) = review.questions(self.db)
        self.assertEqual(question["kind"], review.GITHUB)
        self.assertEqual(question["members"], ["A1", "XieN-N"])
        self.assertEqual(question["evidence"]["login"], "XieN-N")
        self.assertEqual(question["evidence"]["person"], "A1")

    def test_the_question_says_what_is_missing(self):
        self.run_stage(*self.unsure())
        (question,) = review.questions(self.db)
        self.assertEqual(question["evidence"]["held_because"],
                         ["the name matches exactly and nothing else backs it"])

    def test_confirming_it_links_the_account(self):
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.SAME,
                              actor="user:roman")
        result, people = self.run_stage(*self.unsure())
        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "XieN-N")

    def test_rejecting_it_keeps_the_account_off(self):
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.DIFFERENT,
                              actor="user:roman")
        result, people = self.run_stage(*self.unsure())
        self.assertIsNone(people["A1"].github)
        self.assertEqual(result["github_review"], 0)

    def test_an_answered_account_is_not_asked_again(self):
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.DIFFERENT)
        self.run_stage(*self.unsure())
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_a_rejection_holds_against_a_match_the_rules_would_now_make(self):
        # The account carries the surname and owns the repository, which on
        # their own would settle it.
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "shtuka"], review.DIFFERENT,
                              actor="user:roman")
        result, people = self.run_stage(
            [person("A1", "Stanislav Shtuka")],
            [profile("shtuka", name="Stanislav Shtuka", company="ITMO University")])
        self.assertIsNone(people["A1"].github)
        self.assertEqual(result["github_matched"], 0)
        (disputed,) = review.questions(self.db, disputed=True)
        self.assertEqual(disputed["members"], ["A1", "shtuka"])

    def test_an_answer_about_an_account_is_not_an_answer_about_people(self):
        # decisions() feeds the person merge rules. An account paired with a
        # person is not two records of one researcher, and must not reach it.
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.SAME)
        self.assertEqual(review.decisions(self.db), {})
        self.assertEqual(review.github_decisions(self.db),
                         {frozenset({"XieN-N", "A1"}): review.SAME})

    def test_the_journal_marks_what_a_person_decided(self):
        # A row reading "rejected" beside signals that say otherwise needs
        # to say who overruled whom.
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.DIFFERENT)
        self.run_stage(*self.unsure())
        path = self.config.audit_dir / self.prepared.group / MATCHES_FILENAME
        rows = [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]
        (row,) = rows
        self.assertEqual((row["decision"], row["rule"]), ("rejected", "manual"))

    def test_an_answer_survives_its_person_being_folded(self):
        # The dedup merges B into A. The answer is stored about B, and an id
        # nothing carries any more must not lose it, or an account somebody had
        # refused gets linked silently.
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.DIFFERENT,
                              actor="user:katya")
        survivor = person("A2", "Stanislav Shtuka")
        survivor.merged_ids = ["A1"]
        _, people = self.run_stage([survivor], [profile("XieN-N", name="Stanislav Shtuka")])
        self.assertIsNone(people["A2"].github)

    def test_and_the_question_is_not_put_again(self):
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.DIFFERENT)
        survivor = person("A2", "Stanislav Shtuka")
        survivor.merged_ids = ["A1"]
        self.run_stage([survivor], [profile("XieN-N", name="Stanislav Shtuka")])
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_the_question_carries_the_accounts_profile(self):
        self.run_stage([person("A1", "Stanislav Shtuka")],
                       [profile("XieN-N", name="Stanislav Shtuka", company="FEFU",
                                location="Vladivostok")])
        (question,) = review.questions(self.db)
        self.assertEqual(question["evidence"]["profile"],
                         {"name": "Stanislav Shtuka", "company": "FEFU", "location": "Vladivostok"})

    def test_a_name_only_an_unbacked_spelling_gives_asks_nobody(self):
        # Cases from the live queue: an initial expanded by OpenAlex, a namesake lumped in, Cyrillic.
        for main, variant in (("P. V. Gulyaev", "Pavel Gulyaev"), ("Xi Zhang", "Xin Zhang"),
                              ("М. С. Иванов", "Mikhail Ivanov")):
            with self.subTest(main=main):
                self.db[review.COLLECTION].delete_many({})
                result, people = self.run_stage([person("A1", main, variants=[variant])],
                                                [profile("someone", name=variant)])
                self.assertEqual(result["github_review"], 0)
                self.assertIsNone(people["A1"].github)

    def test_a_name_the_main_name_backs_is_still_asked_about(self):
        for main, variants, other in (("Daniil S. Razdyakonov", ["Daniil Razdyakonov"], []),
                                      (None, ["Daniil Razdyakonov"], []),
                                      ("D. S. Razdyakonov", [], ["Daniil Razdyakonov"])):
            with self.subTest(main=main, other=other):
                self.db[review.COLLECTION].delete_many({})
                result, _ = self.run_stage([person("A1", main, variants=variants, other=other)],
                                           [profile("someone", name="Daniil Razdyakonov")])
                self.assertEqual(result["github_review"], 1)

    def test_an_unbacked_spelling_still_links_with_support(self):
        _, people = self.run_stage([person("A1", "A.P. Lapin", variants=["Alexey Lapin"])],
                                   [profile("lapin", name="Alexey Lapin", company="ITMO University")])
        self.assertEqual(people["A1"].github, "lapin")

    def test_a_tie_says_so_in_the_question(self):
        self.run_stage([person("A1", "Stanislav Shtuka"), person("A2", "Stanislav Shtuka")],
                       [profile("XieN-N", name="Stanislav Shtuka", company="ITMO University")])
        (question,) = review.questions(self.db)
        self.assertEqual(question["evidence"]["held_because"],
                         ["several authors fit this account equally well"])

    def test_a_question_asked_again_stays_open(self):
        self.run_stage(*self.unsure())
        self.run_stage(*self.unsure())
        self.assertEqual(review.count(self.db, answered=False), 1)

    def test_a_question_the_rules_now_settle_leaves_the_queue(self):
        self.run_stage(*self.unsure())
        self.run_stage([person("A1", "Stanislav Shtuka")],
                       [profile("XieN-N", name="Stanislav Shtuka", company="ITMO University")])
        self.assertEqual(review.count(self.db, answered=False), 0)
        (closed,) = self.db[review.COLLECTION].find()
        self.assertNotIn("verdict", closed)
        self.assertEqual(closed["auto_resolution"]["decision"], "matched")

    def test_a_question_about_a_folded_person_moves_to_the_survivor(self):
        self.run_stage(*self.unsure())
        survivor = person("A2", "Stanislav Shtuka")
        survivor.merged_ids = ["A1"]
        self.run_stage([survivor], [profile("XieN-N", name="Stanislav Shtuka")])
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A2", "XieN-N"])

    def namesakes(self, *ids, name="Ivan Petrov"):
        """One account and several authors its name fits equally well."""
        return ([person(pid, "Ivan Petrov") for pid in ids], [profile("coder42", name=name)])

    def test_every_author_in_the_running_is_asked_about_at_once(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A1", "A2", "coder42"])
        self.assertEqual(question["evidence"]["names"], ["Ivan Petrov", "Ivan Petrov", "coder42"])

    def test_a_namesake_arriving_later_joins_the_question(self):
        self.run_stage(*self.namesakes("A2"))
        self.run_stage(*self.namesakes("A1", "A2"))
        open_members = [question["members"] for question in review.questions(self.db, answered=False)]
        self.assertEqual(open_members, [["A1", "A2", "coder42"]])

    def test_choosing_an_author_links_that_author(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")
        _, people = self.run_stage(*self.namesakes("A1", "A2"))
        self.assertEqual(people["A2"].github, "coder42")
        self.assertIsNone(people["A1"].github)
        self.assertEqual(review.count(self.db, answered=False), 0)

    def applied_account_choice(self, *, independent=False):
        repo = repository("coder42", "tool")
        first = person("A1", "Ivan Petrov")
        if independent:
            first.github = "orcid-login"
            first.email = "original@lab.test"
            first.contributed_to = [Contribution(repository_id=repo.id, role="owner")]
        self.run_stage(
            [first, person("A2", "Ivan Petrov")],
            [profile("coder42", name="Ivan Petrov", emails=["account@lab.test"], repos=[repo.url])],
            [repo],
        )
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A1")
        self.rerun_stage()
        return repo

    def test_rechoosing_an_account_removes_the_previous_owners_generated_fields(self):
        repo = self.applied_account_choice()
        review.withdraw(self.db, review.GITHUB, ["A1", "A2", "coder42"])
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")

        _, people = self.rerun_stage()

        self.assertIsNone(people["A1"].github)
        self.assertIsNone(people["A1"].email)
        self.assertEqual(people["A1"].contributed_to, [])
        self.assertEqual(people["A2"].github, "coder42")
        self.assertEqual(people["A2"].email, "account@lab.test")
        self.assertEqual(people["A2"].contributed_to,
                         [Contribution(repository_id=repo.id, role="owner")])

    def test_rechoosing_preserves_the_previous_owners_independent_fields(self):
        repo = self.applied_account_choice(independent=True)
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")

        _, people = self.rerun_stage()

        self.assertEqual(people["A1"].github, "orcid-login")
        self.assertEqual(people["A1"].email, "original@lab.test")
        self.assertEqual(people["A1"].contributed_to,
                         [Contribution(repository_id=repo.id, role="owner")])

    def test_an_unchanged_repeat_keeps_the_information_needed_to_reverse_a_choice(self):
        self.applied_account_choice()
        self.rerun_stage()
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")

        _, people = self.rerun_stage()

        self.assertIsNone(people["A1"].github)
        self.assertIsNone(people["A1"].email)
        self.assertEqual(people["A1"].contributed_to, [])

    def test_withdrawing_a_choice_does_not_use_the_copied_email_as_evidence(self):
        self.applied_account_choice()
        review.withdraw(self.db, review.GITHUB, ["A1", "A2", "coder42"])

        result, people = self.rerun_stage()

        self.assertEqual(result["github_matched"], 0)
        self.assertEqual(result["github_review"], 1)
        self.assertTrue(all(author.github is None and author.email is None
                            and not author.contributed_to for author in people.values()))

    def test_a_scoped_rechoice_leaves_the_old_owner_until_their_own_run(self):
        self.applied_account_choice()
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")

        _, people = self.rerun_stage(PreparedSelection("persons", frozenset({"A2"})))

        self.assertEqual(people["A1"].github, "coder42")
        self.assertEqual(people["A2"].github, "coder42")
        _, people = self.rerun_stage(PreparedSelection("persons", frozenset({"A1"})))
        self.assertIsNone(people["A1"].github)
        self.assertEqual(people["A2"].github, "coder42")

    def test_a_group_without_the_profile_does_not_retract_a_previous_match(self):
        repo = self.applied_account_choice()
        self.prepared.write_models("github_profiles", [])

        _, people = self.rerun_stage()

        self.assertEqual(people["A1"].github, "coder42")
        self.assertEqual(people["A1"].email, "account@lab.test")
        self.assertEqual(people["A1"].contributed_to,
                         [Contribution(repository_id=repo.id, role="owner")])

    def test_refusing_one_account_keeps_a_contribution_supported_by_another(self):
        repo = repository("lab", "tool")
        self.run_stage(
            [person("A1", "Ivan Petrov")],
            [profile(login, name="Ivan Petrov", company="ITMO", repos=[repo.url])
             for login in ("alpha", "beta")],
            [repo],
        )
        review.record_verdict(self.db, review.GITHUB, ["A1", "alpha"], review.DIFFERENT)

        _, people = self.rerun_stage()

        self.assertEqual(people["A1"].github, "beta")
        self.assertEqual(people["A1"].contributed_to,
                         [Contribution(repository_id=repo.id, role="contributor")])
        self.assertEqual([match.login for match in people["A1"].github_match.matches], ["beta"])

    def test_a_refusal_can_retract_a_tracked_account_absent_from_this_group(self):
        self.applied_account_choice()
        self.prepared.write_models("github_profiles", [])
        review.record_verdict(self.db, review.GITHUB, ["A1", "A2", "coder42"], review.DIFFERENT)

        _, people = self.rerun_stage()

        self.assertIsNone(people["A1"].github)
        self.assertIsNone(people["A1"].email)
        self.assertEqual(people["A1"].contributed_to, [])

    def test_an_unchanged_match_keeps_contributions_visible_only_to_another_group(self):
        repo = self.applied_account_choice()
        PreparedStore(self.db, "other").upsert_models("repositories", [repo])
        self.prepared.write_models("repositories", [])

        _, people = self.rerun_stage()

        self.assertEqual(people["A1"].contributed_to,
                         [Contribution(repository_id=repo.id, role="owner")])

    def test_an_automatic_match_keeps_evidence_from_a_repository_in_another_group(self):
        repo = repository("coder42", "tool")
        self.run_stage([person("A1", "Ivan Petrov")],
                       [profile("coder42", name="Ivan Petrov", repos=[repo.url])], [repo])
        PreparedStore(self.db, "other").upsert_models("repositories", [repo])
        self.prepared.write_models("repositories", [])

        result, people = self.rerun_stage()

        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "coder42")
        self.assertEqual(people["A1"].contributed_to,
                         [Contribution(repository_id=repo.id, role="owner")])

    def test_a_refreshed_repository_role_replaces_the_previous_role(self):
        repo = self.applied_account_choice()
        repo.owner_login = "new-owner"
        self.prepared.write_models("repositories", [repo])

        _, people = self.rerun_stage()

        self.assertEqual(people["A1"].contributed_to,
                         [Contribution(repository_id=repo.id, role="contributor")])

    def test_rechoosing_after_a_person_fold_removes_the_inherited_match(self):
        self.applied_account_choice()
        people = {author.id: author for author in self.prepared.read_models("persons", Person)}
        survivor = _merge_person(person("A3", "Ivan Petrov"), people["A1"])
        survivor.merged_ids = ["A1"]
        self.prepared.write_models("persons", [survivor, people["A2"]])
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")

        _, people = self.rerun_stage()

        self.assertIsNone(people["A3"].github)
        self.assertIsNone(people["A3"].email)
        self.assertEqual(people["A3"].contributed_to, [])
        self.assertEqual(people["A2"].github, "coder42")

    def test_a_legacy_refusal_removes_the_login_without_guessing_other_origins(self):
        author = person("A1", "Ivan Petrov", github="coder42", email="known@lab.test")
        contribution = Contribution(repository_id="R1", role="owner")
        author.contributed_to = [contribution]
        review.record_verdict(self.db, review.GITHUB, ["A1", "coder42"], review.DIFFERENT)

        for profiles in ([profile("coder42", name="Ivan Petrov")], []):
            with self.subTest(profile_available=bool(profiles)):
                _, people = self.run_stage([author], profiles)

                self.assertIsNone(people["A1"].github)
                self.assertEqual(people["A1"].email, "known@lab.test")
                self.assertEqual(people["A1"].contributed_to, [contribution])

    def test_a_choice_survives_the_chosen_author_being_folded(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")
        survivor = person("A3", "Ivan Petrov")
        survivor.merged_ids = ["A2"]
        _, people = self.run_stage([person("A1", "Ivan Petrov"), survivor],
                                   [profile("coder42", name="Ivan Petrov")])
        self.assertEqual(people["A3"].github, "coder42")

    def test_none_of_them_links_nobody_and_asks_nothing(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        review.record_verdict(self.db, review.GITHUB, ["A1", "A2", "coder42"], review.DIFFERENT)
        result, _ = self.run_stage(*self.namesakes("A1", "A2"))
        self.assertEqual((result["github_matched"], result["github_review"]), (0, 0))
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_after_none_of_them_only_a_new_author_is_asked(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        review.record_verdict(self.db, review.GITHUB, ["A1", "A2", "coder42"], review.DIFFERENT)
        self.run_stage(*self.namesakes("A1", "A2", "A3"))
        open_members = [question["members"] for question in review.questions(self.db, answered=False)]
        self.assertEqual(open_members, [["A3", "coder42"]])

    def test_none_of_them_is_flagged_when_the_rules_would_now_link_one(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        review.record_verdict(self.db, review.GITHUB, ["A1", "A2", "coder42"], review.DIFFERENT)
        _, people = self.run_stage(
            [person("A1", "Ivan Petrov", email="ivan@itmo.ru"), person("A2", "Ivan Petrov")],
            [profile("coder42", name="Ivan Petrov", emails=["ivan@itmo.ru"])])
        self.assertIsNone(people["A1"].github)
        (disputed,) = review.questions(self.db, disputed=True)
        self.assertEqual(disputed["members"], ["A1", "A2", "coder42"])

    def test_none_of_them_over_a_tie_is_not_flagged(self):
        # The rules alone would ask about a tie, not link either author.
        self.run_stage(*self.namesakes("A1", "A2"))
        review.record_verdict(self.db, review.GITHUB, ["A1", "A2", "coder42"], review.DIFFERENT)
        self.run_stage([person("A1", "Ivan Petrov"), person("A2", "Ivan Petrov")],
                       [profile("coder42", name="Ivan Petrov", company="ITMO University")])
        self.assertEqual(review.count(self.db, disputed=True), 0)

    def test_the_later_of_two_answers_about_one_author_wins(self):
        # A page left open can answer the single-author question after the joint one.
        early, late = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        for yes_at, linked in ((early, None), (late, "coder42")):
            with self.subTest(linked=linked):
                self.db[review.COLLECTION].delete_many({})
                review.record_verdict(self.db, review.GITHUB, ["A1", "coder42"], review.SAME)
                review.record_verdict(self.db, review.GITHUB, ["A1", "A2", "coder42"],
                                      review.DIFFERENT)
                self.db[review.COLLECTION].update_one(
                    {"_id": "github_person:A1:coder42"}, {"$set": {"decided_at": yes_at}})
                self.db[review.COLLECTION].update_one(
                    {"_id": "github_person:A1:A2:coder42"},
                    {"$set": {"decided_at": late if yes_at == early else early}})
                _, people = self.run_stage(*self.namesakes("A1", "A2"))
                self.assertEqual(people["A1"].github, linked)

    def test_after_a_no_the_next_author_is_asked(self):
        # The answer was given when the account was asked about one author at a time.
        both = ([person("A1", "Ivan Petrov"), person("A2", "Ivan Petrov")],
                [profile("coder42", name="Ivan Petrov")])
        self.run_stage(*both)
        review.record_verdict(self.db, review.GITHUB, ["A1", "coder42"], review.DIFFERENT)
        self.run_stage(*both)
        open_members = [question["members"] for question in review.questions(self.db, answered=False)]
        self.assertEqual(open_members, [["A2", "coder42"]])

    def test_refusing_one_author_does_not_hide_another_with_the_same_address(self):
        self.run_stage(
            [person("A1", "Ivan Petrov", email="shared@itmo.ru"),
             person("A2", "Boris Smirnov", email="shared@itmo.ru")],
            [profile("coder42", emails=["shared@itmo.ru"])],
        )
        review.record_verdict(self.db, review.GITHUB, ["A1", "coder42"], review.DIFFERENT)

        result, people = self.rerun_stage()

        self.assertEqual(result["github_matched"], 1)
        self.assertIsNone(people["A1"].github)
        self.assertEqual(people["A2"].github, "coder42")
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_a_yes_about_a_tied_author_links_that_author(self):
        self.run_stage([person("A2", "Ivan Petrov")], [profile("coder42", name="Ivan Petrov")])
        both = ([person("A1", "Ivan Petrov"), person("A2", "Ivan Petrov")],
                [profile("coder42", name="Ivan Petrov")])
        self.run_stage(*both)
        review.record_verdict(self.db, review.GITHUB, ["A2", "coder42"], review.SAME)
        _, people = self.run_stage(*both)
        self.assertEqual(people["A2"].github, "coder42")
        self.assertIsNone(people["A1"].github)
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_a_yes_holds_where_the_signals_alone_would_reject(self):
        # Found only through the author's paper, under a nickname: the rules see no name.
        review.record_verdict(self.db, review.GITHUB, ["A1", "nick"], review.SAME)
        _, people = self.run_stage(
            [person("A1", "Ivan Petrov", works=["W1"])],
            [profile("nick", name="Night Owl", repos=["https://github.com/lab/tool"])],
            [repository("lab", "tool", works=["W1"])])
        self.assertEqual(people["A1"].github, "nick")

    def test_a_confirmation_survives_the_accounts_display_name_changing(self):
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.SAME)
        self.prepared.write_models("github_profiles", [profile("XieN-N", name="Night Owl")])

        result, people = self.rerun_stage()

        self.assertEqual(result["github_matched"], 1)
        self.assertEqual(people["A1"].github, "XieN-N")
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_a_choice_without_remaining_signals_still_considers_a_new_rival(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        review.record_account_choice(self.db, "coder42", ["A1", "A2"], "A2")
        self.prepared.upsert_models("persons", [person("A3", "Boris Smirnov", email="boris@itmo.ru")])
        self.prepared.write_models("github_profiles", [
            profile("coder42", name="Night Owl", emails=["boris@itmo.ru"]),
        ])

        result, people = self.rerun_stage()

        self.assertEqual(result["github_matched"], 0)
        self.assertTrue(all(author.github is None for author in people.values()))
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A2", "A3", "coder42"])
        self.assertEqual(question["evidence"]["held_because"], [
            "answered before, but the question did not name every author that fits",
        ])

    def test_a_confirmation_without_remaining_signals_follows_a_folded_author(self):
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.SAME)
        survivor = person("A2", "Stanislav Shtuka")
        survivor.merged_ids = ["A1"]
        self.prepared.write_models("persons", [survivor])
        self.prepared.write_models("github_profiles", [profile("XieN-N", name="Night Owl")])

        _, people = self.rerun_stage()

        self.assertEqual(people["A2"].github, "XieN-N")

    def kokorin(self):
        """Two namesakes; the account shares an address with the second."""
        return ([person("A1", "Ilya Kokorin"), person("A2", "Ilya Kokorin", email="ilya@itmo.ru")],
                [profile("KokorinIlya", name="Ilya Kokorin", emails=["ilya@itmo.ru"])])

    def nikitin(self):
        """Namesakes the rules like equally, one only through an OpenAlex spelling."""
        return ([person("A1", "N. V. Nikitin", variants=["Nikolay Nikitin"]),
                 person("A2", "Nikolay O. Nikitin", variants=["Nikolay Nikitin"])],
                [profile("nicl-nno", name="Nikolay Nikitin", company="ITMO University")])

    def said_yes_long_ago(self, members):
        """A "yes" from when the account was asked about one author only."""
        review.record_verdict(self.db, review.GITHUB, members, review.SAME)
        self.db[review.COLLECTION].update_one(
            {"_id": review.question_id(review.GITHUB, members)},
            {"$set": {"decided_at": datetime(2026, 1, 1, tzinfo=UTC)}})

    def test_a_yes_does_not_beat_an_author_the_rules_like_better(self):
        # Live case: the yes named a namesake at Leiden; the address points at the ITMO one.
        self.said_yes_long_ago(["A1", "KokorinIlya"])
        _, people = self.run_stage(*self.kokorin())
        self.assertEqual((people["A1"].github, people["A2"].github), (None, None))
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A1", "A2", "KokorinIlya"])
        self.assertEqual(question["evidence"]["held_because"],
                         ["answered before, but the question did not name every author that fits"])

    def test_a_yes_does_not_beat_an_author_the_rules_like_as_much(self):
        # Live case: the yes named Nikolay V. Nikitin of Moscow, the account is Nikolay O.'s.
        self.said_yes_long_ago(["A1", "nicl-nno"])
        _, people = self.run_stage(*self.nikitin())
        self.assertEqual((people["A1"].github, people["A2"].github), (None, None))
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A1", "A2", "nicl-nno"])

    def test_choosing_settles_what_the_yes_left_open(self):
        for chosen in ("A1", "A2"):
            with self.subTest(chosen=chosen):
                self.db[review.COLLECTION].delete_many({})
                self.said_yes_long_ago(["A1", "KokorinIlya"])
                self.run_stage(*self.kokorin())
                review.record_account_choice(self.db, "KokorinIlya", ["A1", "A2"], chosen)
                _, people = self.run_stage(*self.kokorin())
                self.assertEqual({pid for pid in ("A1", "A2") if people[pid].github}, {chosen})
                self.assertEqual(review.count(self.db, answered=False), 0)

    def test_choosing_against_the_rules_is_flagged(self):
        self.run_stage(*self.kokorin())
        review.record_account_choice(self.db, "KokorinIlya", ["A1", "A2"], "A1")
        self.run_stage(*self.kokorin())
        (disputed,) = review.questions(self.db, disputed=True)
        self.assertEqual(disputed["members"], ["A1", "A2", "KokorinIlya"])

    def test_a_choice_of_the_refused_author_is_not_flagged(self):
        # Chosen first, refused later: only the refusal disagrees with the rules.
        review.record_account_choice(self.db, "KokorinIlya", ["A1", "A2"], "A2")
        review.record_verdict(self.db, review.GITHUB, ["A2", "KokorinIlya"], review.DIFFERENT)
        for members, day in ((["A1", "A2", "KokorinIlya"], 1), (["A2", "KokorinIlya"], 2)):
            self.db[review.COLLECTION].update_one(
                {"_id": review.question_id(review.GITHUB, members)},
                {"$set": {"decided_at": datetime(2026, 1, day, tzinfo=UTC)}})
        self.run_stage(*self.kokorin())
        (disputed,) = review.questions(self.db, disputed=True)
        self.assertEqual(disputed["members"], ["A2", "KokorinIlya"])

    def test_a_scoped_run_does_not_close_a_question_it_did_not_apply(self):
        # The rules would now link A1, but a run limited to B1 writes nothing to A1.
        self.run_stage(*self.unsure())
        _, people = self.run_stage(
            [person("A1", "Stanislav Shtuka"), person("B1", "Boris Ivanov")],
            [profile("XieN-N", name="Stanislav Shtuka", company="ITMO University")],
            selection=PreparedSelection("persons", frozenset({"B1"})))
        self.assertIsNone(people["A1"].github)
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A1", "XieN-N"])

    def test_a_scoped_run_leaves_questions_about_others_alone(self):
        # The account no longer fits A1, but a run limited to B1 does not speak for A1.
        self.run_stage(*self.unsure())
        self.run_stage([person("A1", "Stanislav Shtuka"), person("B1", "Boris Ivanov")],
                       [profile("XieN-N", name="Ivan Petrov")],
                       selection=PreparedSelection("persons", frozenset({"B1"})))
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A1", "XieN-N"])

    def test_a_scoped_run_that_writes_no_link_keeps_the_accounts_questions(self):
        # The rules now give the account to A1, but a run limited to B1 writes nothing to A1.
        self.run_stage(*self.namesakes("B1"))
        self.run_stage([person("A1", "Ivan Petrov", email="ivan@itmo.ru"), person("B1", "Ivan Petrov")],
                       [profile("coder42", name="Ivan Petrov", emails=["ivan@itmo.ru"])],
                       selection=PreparedSelection("persons", frozenset({"B1"})))
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["B1", "coder42"])

    def test_a_scoped_run_keeps_a_question_naming_someone_outside_it(self):
        self.run_stage(*self.namesakes("A1", "A2"))
        self.run_stage(*self.namesakes("A1", "A2", name="Boris Ivanov"),
                       selection=PreparedSelection("persons", frozenset({"A1"})))
        (question,) = review.questions(self.db, answered=False)
        self.assertEqual(question["members"], ["A1", "A2", "coder42"])

    def test_a_question_about_someone_the_run_never_saw_stays(self):
        self.run_stage(*self.unsure())
        other = PreparedStore(self.db, "other")
        other.write_models("persons", [person("B1", "Ivan Petrov")])
        other.write_models("github_profiles", [profile("XieN-N", name="Stanislav Shtuka")])
        GitHubMatchStage(other, RawStore(self.db, "other"), self.config).run()
        self.assertEqual(review.count(self.db, answered=False), 1)

    def test_an_answered_question_is_never_closed(self):
        self.run_stage(*self.unsure())
        review.record_verdict(self.db, review.GITHUB, ["A1", "XieN-N"], review.SAME)
        self.run_stage([person("A1", "Stanislav Shtuka")], [profile("XieN-N", name="Ivan Petrov")])
        (answered,) = review.questions(self.db, answered=True)
        self.assertNotIn("auto_resolved_at", answered)


if __name__ == "__main__":
    unittest.main()
