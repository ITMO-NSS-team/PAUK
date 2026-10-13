import unittest
from unittest.mock import patch

import mongomock

from pauk.graph.jsonl_loader import extract_repo_links
from pauk.models import CodeLink, GitHubProfile, Publication, RepoLink, Repository
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.code_links import _collect_occurrences, _occurrences_in_text
from pauk.pipeline.stages.link_relevance import _update_publication_code
from pauk.pipeline.stages.repo_people import RepoPeopleStage
from pauk.pipeline.stages.repositories import RepositoriesStage
from pauk.sources.base import HttpRequestError
from pauk.storage import PreparedStore, RawStore


class UrlAlternativesTest(unittest.TestCase):
    SHORT = "https://github.com/org/evo_network_"
    FULL = SHORT + "public"

    def collect(self, *pages, abstract=""):
        return _collect_occurrences(abstract, [
            _occurrences_in_text(text, number) for number, text in enumerate(pages, 1)
        ])

    def links(self, occurrences):
        return [CodeLink(url=url, occurrences=items, is_relevant=True)
                for url, items in occurrences.items()]

    def test_underscore_keeps_both_candidates_and_original_fragment(self):
        raw = self.SHORT + "\npublic"
        found = self.collect(raw)
        self.assertEqual(set(found), {self.SHORT, self.FULL})
        for link in self.links(found):
            self.assertTrue(link.url_ambiguous)
            self.assertEqual(link.occurrences[0].raw_url, raw)
            self.assertEqual(set(link.occurrences[0].candidate_urls), set(found))

    def test_continuous_spelling_on_another_page_resolves_both_orders(self):
        for pages in [(self.SHORT + "\npublic", self.FULL),
                      (self.FULL, self.SHORT + "\npublic")]:
            with self.subTest(pages=pages):
                found = self.collect(*pages)
                self.assertEqual(list(found), [self.FULL])
                self.assertEqual(len(found[self.FULL]), 2)
                self.assertFalse(self.links(found)[0].url_ambiguous)

    def test_continuous_spelling_on_same_page_resolves_both_orders(self):
        for text in [self.SHORT + "\npublic text " + self.FULL,
                     self.FULL + " text " + self.SHORT + "\npublic"]:
            with self.subTest(text=text):
                found = self.collect(text)
                self.assertEqual(list(found), [self.FULL])
                self.assertIn(self.SHORT + "\npublic", found[self.FULL][0].raw_fragments)
                self.assertIn(self.FULL, found[self.FULL][0].raw_fragments)

    def test_both_direct_spellings_keep_both(self):
        found = self.collect(self.SHORT + "\npublic", self.SHORT + " and " + self.FULL)
        self.assertEqual(set(found), {self.SHORT, self.FULL})
        self.assertTrue(all(not link.url_ambiguous for link in self.links(found)))

    def test_another_wrapped_occurrence_cannot_confirm(self):
        found = self.collect(self.SHORT + "\npublic", self.SHORT + "\npublic")
        self.assertEqual(set(found), {self.SHORT, self.FULL})
        self.assertTrue(all(link.url_ambiguous for link in self.links(found)))

    def test_abstract_and_different_owner_cannot_resolve_pdf(self):
        found = self.collect(self.SHORT + "\npublic", self.FULL.replace("/org/", "/other/"),
                             abstract=self.FULL)
        self.assertIn(self.SHORT, found)
        self.assertEqual(len(found[self.SHORT][0].candidate_urls), 2)

    def test_hyphen_preserved_when_confirmed_by_continuous_text(self):
        found = self.collect("github.com/org/my-\nrepo", "github.com/org/my-repo")
        self.assertEqual(list(found), ["https://github.com/org/my-repo"])

    def test_removed_hyphen_can_also_be_confirmed(self):
        found = self.collect("github.com/org/my-\nrepo", "github.com/org/myrepo")
        self.assertEqual(list(found), ["https://github.com/org/myrepo"])

    def test_confirmation_compares_owner_and_repo_not_scheme_or_case(self):
        found = self.collect("github.com/org/my-\nrepo", "http://www.github.com/ORG/MY-REPO")
        self.assertNotIn("https://github.com/org/myrepo", found)
        self.assertIn("https://github.com/org/my-repo", found)
        self.assertTrue(all(not link.url_ambiguous for link in self.links(found)))

    def test_does_not_cross_blank_line_or_page(self):
        self.assertEqual(list(self.collect(self.SHORT + "\n\npublic")), [self.SHORT])
        self.assertEqual(list(self.collect(self.SHORT, "public")), [self.SHORT])

    def test_multiple_breaks_preserve_all_combinations(self):
        found = self.collect("github.com/my-\norg/my-\nrepo")
        self.assertEqual(len(found), 4)

    def test_resolving_one_fragment_does_not_discard_another_ambiguous_mention(self):
        found = self.collect(
            "github.com/org/a-\nbc and github.com/org/ab-\nc",
            "github.com/org/a-bc",
        )
        self.assertEqual(set(found), {"https://github.com/org/abc",
                                      "https://github.com/org/a-bc", "https://github.com/org/ab-c"})
        by_url = {link.url: link for link in self.links(found)}
        self.assertTrue(by_url["https://github.com/org/abc"].url_ambiguous)
        self.assertFalse(by_url["https://github.com/org/a-bc"].url_ambiguous)

    def test_ambiguous_urls_do_not_become_author_artifacts(self):
        links = self.links(self.collect(self.SHORT + "\npublic"))
        publication = Publication(id="W1", title="Test")
        _update_publication_code(publication, links)
        self.assertFalse(publication.has_code)
        self.assertIsNone(publication.code_url)
        row = RepoLink(publication_id="W1", links=links)
        _, _, edges, _ = extract_repo_links(row.model_dump(), {})
        self.assertEqual(len(edges), 2)
        for _, _, props in edges:
            self.assertTrue(props["url_ambiguous"])
            self.assertIsNone(props["is_relevant"])

    def test_github_checks_both_once_and_preserves_temporary_failures(self):
        for status_code, expected in [(404, "not_found"), (403, "failed"), (502, "failed")]:
            with self.subTest(status_code=status_code):
                db = mongomock.MongoClient()["test"]
                prepared = PreparedStore(db, "test")
                links = self.links(self.collect(self.SHORT + "\npublic"))
                prepared.write_models("repo_links", [
                    RepoLink(publication_id=pub, links=links) for pub in ("W1", "W2")
                ])

                def fetch(owner, name, status_code=status_code):
                    if name == "evo_network_":
                        raise HttpRequestError("GET", self.SHORT, status_code=status_code)
                    return {"name": name, "html_url": self.FULL, "id": 42}

                with patch("pauk.pipeline.stages.repositories.GitHubClient") as client:
                    client.return_value.get_repository.side_effect = fetch
                    client.return_value.has_readme.return_value = True
                    RepositoriesStage(prepared, RawStore(db, "test")).run()
                    self.assertEqual(client.return_value.get_repository.call_count, 2)
                for row in prepared.read_models("repo_links", RepoLink):
                    by_url = {link.url: link for link in row.links}
                    self.assertEqual(by_url[self.SHORT].availability, expected)
                    self.assertEqual(by_url[self.FULL].availability, "available")
                    self.assertTrue(by_url[self.FULL].url_ambiguous)
                self.assertTrue(all(not repo.publication_ids
                                    for repo in prepared.read_models("repositories", Repository)))


class ProfileFailureTest(unittest.TestCase):
    def test_failure_preserves_profiles_and_retry_skips_successful_accounts(self):
        db = mongomock.MongoClient()["test"]
        prepared = PreparedStore(db, "test")
        raw = RawStore(db, "test")
        prepared.write_models("repositories", [Repository(
            id="github_alice_tool", name="tool", url="https://github.com/alice/tool",
            owner_login="alice",
        )])
        prepared.write_models("github_profiles", [GitHubProfile(
            id="github_bob", login="bob", name="Existing name",
            emails=["commit@example.org"], commit_names=["Commit name"],
        )])
        with patch("pauk.pipeline.stages.repo_people.GitHubClient") as client:
            api = client.return_value
            api.contributors.return_value = [{"login": "bob", "type": "User"}]
            api.commits.return_value = []
            api.get_user.side_effect = [{"login": "alice", "name": "Alice"}, RuntimeError("502")]
            RepoPeopleStage(prepared, raw).run()
            repo = next(prepared.read_models("repositories", Repository))
            self.assertEqual(repo.processing["repo_people"].status, ProcessingStatus.FAILED)
            profiles = {p.login: p for p in prepared.read_models("github_profiles", GitHubProfile)}
            self.assertTrue(profiles["alice"].profile_fetched)
            self.assertEqual(profiles["bob"].name, "Existing name")
            self.assertFalse(profiles["bob"].profile_fetched)
            self.assertEqual(len(list(raw.read("github_user"))), 1)

            api.get_user.reset_mock(side_effect=True)
            api.get_user.return_value = {"login": "bob", "name": "Bob"}
            RepoPeopleStage(prepared, raw).run()
            api.get_user.assert_called_once_with("bob")
            repo = next(prepared.read_models("repositories", Repository))
            self.assertEqual(repo.processing["repo_people"].status, ProcessingStatus.COMPLETED)

            api.get_user.reset_mock()
            api.get_user.side_effect = RuntimeError("502 during refresh")
            RepoPeopleStage(prepared, raw, force=True).run()
            profiles = {p.login: p for p in prepared.read_models("github_profiles", GitHubProfile)}
            self.assertEqual(profiles["alice"].name, "Alice")
            self.assertTrue(profiles["alice"].profile_fetched)
            repo = next(prepared.read_models("repositories", Repository))
            self.assertEqual(repo.processing["repo_people"].status, ProcessingStatus.FAILED)
