import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import fitz
import mongomock

from pauk.models import (
    ClassificationStatus,
    CodeLink,
    GitHubProfile,
    LinkOccurrence,
    Publication,
    RepoLink,
    Repository,
)
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.code_links import (
    CodeLinksStage,
    _collect_occurrences,
    _normalize_ligatures,
    _occurrences_in_text,
)
from pauk.pipeline.stages.link_relevance import LinkRelevanceStage
from pauk.pipeline.stages.repo_people import RepoPeopleStage, _is_person
from pauk.pipeline.stages.repositories import RepositoriesStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


def _make_pdf_bytes(pages_text: list[str]) -> bytes:
    """A tiny real PDF, built in-memory, so tests don't need a network call or a binary fixture file."""
    doc = fitz.open()
    for text in pages_text:
        doc.new_page().insert_text((72, 72), text)
    try:
        return doc.tobytes()
    finally:
        doc.close()


def _make_pdf_with_hyperlink(visible_text: str, uri: str, page_text: str = "") -> bytes:
    """A one-page PDF where `visible_text` also carries a clickable link
    annotation pointing at `uri`; `page_text` (if given) is separate body text."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), visible_text)
    page.insert_link({"kind": fitz.LINK_URI, "uri": uri, "from": fitz.Rect(70, 60, 400, 76)})
    if page_text:
        page.insert_text((72, 100), page_text)
    try:
        return doc.tobytes()
    finally:
        doc.close()


class StagesTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_code_links_marks_empty_and_found_results(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="with code", abstract="https://github.com/org/repo"),
            Publication(id="W2", title="without code"),
        ])
        CodeLinksStage(prepared, raw).run()
        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)
        self.assertFalse(rows["W1"].has_code)
        self.assertIsNone(rows["W1"].code_url)
        links = {r.publication_id: r for r in prepared.read_models("repo_links", RepoLink)}
        # code_links only records what was found; whether it's the
        # authors' own artifact is link_relevance's call, not this stage's.
        link = links["W1"].links[0]
        self.assertEqual(link.classification_status, ClassificationStatus.PENDING)
        self.assertIsNone(link.is_relevant)

    def test_code_links_strips_sentence_ending_period_from_url(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", abstract="Code is available at https://github.com/org/repo."),
        ])
        CodeLinksStage(prepared, raw).run()
        links = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        self.assertEqual([link.url for link in links], ["https://github.com/org/repo"])

    def test_code_links_canonicalizes_www_github_host(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", abstract="https://www.github.com/org/repo"),
        ])
        CodeLinksStage(prepared, raw).run()
        links = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        self.assertEqual([link.url for link in links], ["https://github.com/org/repo"])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_forced_repository_enrichment_fetches_each_repository_once(self, github_client):
        github_client.return_value.get_repository.return_value = {
            "html_url": "https://github.com/org/repo", "name": "repo", "owner": {"login": "org"},
        }
        # Without this the stage stores the MagicMock itself in Repository.has_readme,
        # which is typed bool — the row still round-trips, but as a mock repr.
        github_client.return_value.has_readme.return_value = True
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
            RepoLink(publication_id="W2", links=[CodeLink(url="https://www.github.com/org/repo")]),
        ])
        RepositoriesStage(prepared, raw, force=True).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_repositories_enriches_every_mention_but_only_links_authors_artifacts(
        self, github_client,
    ):
        github_client.return_value.get_repository.return_value = {
            "html_url": "https://github.com/org/repo",
            "name": "repo",
            "owner": {"login": "org", "type": "Organization"},
        }
        github_client.return_value.has_readme.return_value = True
        github_client.return_value.contributors.return_value = []
        github_client.return_value.commits.return_value = []
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(
                url="https://github.com/org/repo", is_relevant=True,
            )]),
            RepoLink(publication_id="W2", links=[CodeLink(
                url="https://github.com/org/repo", is_relevant=False,
            )]),
        ])

        RepositoriesStage(prepared, raw).run()

        repository = next(prepared.read_models("repositories", Repository))
        self.assertEqual(repository.publication_ids, ["W1"])
        self.assertEqual(repository.cited_urls, ["https://github.com/org/repo"])
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_repositories_removes_a_stale_implementation_but_keeps_other_groups(
        self, github_client,
    ):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("repositories", [Repository(
            id="github_org_repo",
            name="repo",
            url="https://github.com/org/repo",
            publication_ids=["W1", "W-outside-this-group"],
            processing={
                "repositories": ProcessingState(status=ProcessingStatus.COMPLETED),
            },
        )])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(
                url="https://github.com/org/repo", is_relevant=False,
            )]),
        ])

        RepositoriesStage(prepared, raw).run()

        repository = next(prepared.read_models("repositories", Repository))
        self.assertEqual(repository.publication_ids, ["W-outside-this-group"])
        github_client.return_value.get_repository.assert_not_called()

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_repositories_preserves_claim_without_a_matching_discovered_link(
        self, github_client,
    ):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        completed = ProcessingState(status=ProcessingStatus.COMPLETED)
        prepared.write_models("repositories", [
            Repository(
                id="github_org_curated",
                name="curated",
                url="https://github.com/org/curated",
                publication_ids=["W1"],
                processing={"repositories": completed},
            ),
            Repository(
                id="github_org_mentioned",
                name="mentioned",
                url="https://github.com/org/mentioned",
                processing={"repositories": completed},
            ),
        ])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(
                url="https://github.com/org/mentioned", is_relevant=False,
            )]),
        ])

        RepositoriesStage(prepared, raw).run()

        repositories = {
            repository.id: repository
            for repository in prepared.read_models("repositories", Repository)
        }
        self.assertEqual(repositories["github_org_curated"].publication_ids, ["W1"])
        self.assertEqual(repositories["github_org_mentioned"].publication_ids, [])
        github_client.return_value.get_repository.assert_not_called()

    def test_force_reprocesses_completed_rows(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="with code", abstract="https://github.com/org/repo"),
        ])
        CodeLinksStage(prepared, raw).run()
        result = CodeLinksStage(prepared, raw).run()
        self.assertEqual(result["publications"], 0)  # completed rows are skipped
        result = CodeLinksStage(prepared, raw, force=True).run()
        self.assertEqual(result["publications"], 1)
        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["code_links"].attempts, 2)

    def test_software_deposit_links_to_the_repository_it_archives(self):
        # Zenodo mints a DOI per GitHub release, so the archive shows up as a
        # work of its own; the repository it archives is named in the title.
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="asl/BandageNG: Continuous build", type="software"),
            Publication(id="W2", title="A/B testing: what we measured", type="dataset"),
            Publication(id="W3", title="ablab/spades: Release v4.3.0", type="article"),
        ])
        CodeLinksStage(prepared, raw).run()
        LinkRelevanceStage(prepared, raw).run()
        rows = {r.id: r for r in prepared.read_models("publications", Publication)}
        self.assertEqual(json.loads(rows["W1"].code_url), ["https://github.com/asl/BandageNG"])
        links = {r.publication_id: r for r in prepared.read_models("repo_links", RepoLink)}
        self.assertEqual(links["W1"].links[0].llm_reason, "repository_archived_by_this_deposit")
        self.assertEqual(
            links["W1"].links[0].classification_status,
            ClassificationStatus.CLASSIFIED,
        )
        # A title with a space before the colon is prose, not owner/name,
        # and a plain article is never read as an archive.
        self.assertIsNone(rows["W2"].code_url)
        self.assertIsNone(rows["W3"].code_url)

    def test_code_links_invalidates_a_verdict_based_on_previous_contexts(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(
            id="W1",
            title="paper",
            abstract="https://github.com/org/repo",
            processing={
                "link_relevance": ProcessingState(status=ProcessingStatus.COMPLETED),
            },
        )])

        CodeLinksStage(prepared, raw).run()

        publication = next(prepared.read_models("publications", Publication))
        self.assertNotIn("link_relevance", publication.processing)

    def test_legacy_link_with_an_explicit_uncertain_verdict_is_classified(self):
        classified = CodeLink.model_validate({
            "url": "https://github.com/org/repo",
            "is_relevant": None,
            "llm_confidence": 0.3,
            "llm_reason": "insufficient context",
        })
        pending = CodeLink.model_validate({"url": "https://github.com/org/other"})

        self.assertEqual(classified.classification_status, ClassificationStatus.CLASSIFIED)
        self.assertEqual(pending.classification_status, ClassificationStatus.PENDING)

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_classifies_pending_links(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": True, "confidence": 0.9, "reason": "authors say so",
        }
        openrouter_client.return_value.last_response = {"choices": []}
        openrouter_client.return_value.last_usage = None
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])
        LinkRelevanceStage(prepared, raw).run()
        rows = {r.id: r for r in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["link_relevance"].status, ProcessingStatus.COMPLETED)
        link = next(prepared.read_models("repo_links", RepoLink)).links[0]
        self.assertEqual(link.classification_status, ClassificationStatus.CLASSIFIED)
        self.assertTrue(link.is_relevant)
        self.assertEqual(link.llm_confidence, 0.9)
        self.assertEqual(link.llm_reason, "authors say so")
        self.assertTrue(rows["W1"].has_code)
        self.assertEqual(json.loads(rows["W1"].code_url), ["https://github.com/org/repo"])

    def test_link_relevance_stores_all_authors_repositories_in_discovery_order(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [RepoLink(publication_id="W1", links=[
            CodeLink(
                url="https://github.com/org/first",
                is_relevant=True,
                llm_confidence=0.6,
                llm_reason="authors' repository",
            ),
            CodeLink(
                url="https://github.com/org/third-party",
                is_relevant=False,
                llm_confidence=1.0,
                llm_reason="dependency",
            ),
            CodeLink(
                url="https://github.com/org/best",
                is_relevant=True,
                llm_confidence=0.9,
                llm_reason="authors' repository",
            ),
        ])])

        LinkRelevanceStage(prepared, raw).run()

        publication = next(prepared.read_models("publications", Publication))
        self.assertTrue(publication.has_code)
        self.assertEqual(
            json.loads(publication.code_url),
            ["https://github.com/org/first", "https://github.com/org/best"],
        )

    def test_link_relevance_does_not_count_false_or_uncertain_links_as_code(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(
            id="W1", title="paper", has_code=True, code_url="https://github.com/org/stale",
        )])
        prepared.write_models("repo_links", [RepoLink(publication_id="W1", links=[
            CodeLink(
                url="https://github.com/org/dependency",
                is_relevant=False,
                llm_confidence=0.9,
                llm_reason="dependency",
            ),
            CodeLink(
                url="https://github.com/org/uncertain",
                is_relevant=None,
                llm_confidence=0.2,
                llm_reason="insufficient context",
            ),
        ])])

        LinkRelevanceStage(prepared, raw).run()

        publication = next(prepared.read_models("publications", Publication))
        self.assertFalse(publication.has_code)
        self.assertIsNone(publication.code_url)

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_sends_every_occurrence_in_one_prompt(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": True, "confidence": 0.9, "reason": "later context confirms it",
        }
        openrouter_client.return_value.last_response = {"choices": []}
        openrouter_client.return_value.last_usage = None
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(
                url="https://github.com/org/repo",
                occurrences=[
                    LinkOccurrence(context="We use this library as a dependency."),
                    LinkOccurrence(context="Our complete implementation is available here.", page_number=7),
                ],
            )]),
        ])

        LinkRelevanceStage(prepared, raw).run()

        [call] = openrouter_client.return_value.chat_json.call_args_list
        prompt = call.args[0]
        self.assertIn("We use this library as a dependency.", prompt)
        self.assertIn("Our complete implementation is available here.", prompt)
        self.assertIn("абстракт OpenAlex", prompt)
        self.assertIn("страница 7", prompt)

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_keeps_an_explicit_uncertain_verdict(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": None, "confidence": 0.3, "reason": "insufficient context",
        }
        openrouter_client.return_value.last_response = {"choices": []}
        openrouter_client.return_value.last_usage = None
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])

        LinkRelevanceStage(prepared, raw).run()

        rows = {r.id: r for r in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["link_relevance"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(rows["W1"].processing["link_relevance"].result_count, 1)
        link = next(prepared.read_models("repo_links", RepoLink)).links[0]
        self.assertEqual(link.classification_status, ClassificationStatus.CLASSIFIED)
        self.assertIsNone(link.is_relevant)
        self.assertEqual(link.llm_confidence, 0.3)
        self.assertEqual(link.llm_reason, "insufficient context")

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_rejects_a_non_boolean_verdict(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": "false", "confidence": 0.8, "reason": "wrong JSON type",
        }
        openrouter_client.return_value.last_response = {"choices": []}
        openrouter_client.return_value.last_usage = None
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])

        LinkRelevanceStage(prepared, raw).run()

        rows = {r.id: r for r in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["link_relevance"].status, ProcessingStatus.FAILED)
        link = next(prepared.read_models("repo_links", RepoLink)).links[0]
        self.assertEqual(link.classification_status, ClassificationStatus.FAILED)
        self.assertIsNone(link.is_relevant)
        [log] = list(self.db["llm_logs_link_relevance"].find({}))
        self.assertEqual(log["error"], "is_authors_artifact must be true, false, or null")

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_logs_every_llm_call(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": True, "confidence": 0.9, "reason": "authors say so",
        }
        openrouter_client.return_value.last_response = {"choices": [{"message": {"content": "..."}}]}
        openrouter_client.return_value.last_usage = {"total_tokens": 42}
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])
        LinkRelevanceStage(prepared, raw).run()

        [log] = list(self.db["llm_logs_link_relevance"].find({}))
        self.assertEqual(log["group"], "sample")
        self.assertIn("https://github.com/org/repo", log["prompt"])
        self.assertEqual(log["raw_response"], {"choices": [{"message": {"content": "..."}}]})
        self.assertEqual(log["parsed"], {"is_authors_artifact": True, "confidence": 0.9, "reason": "authors say so"})
        self.assertEqual(log["usage"], {"total_tokens": 42})
        self.assertIsNone(log["error"])
        self.assertEqual(log["context"], {"publication_id": "W1", "url": "https://github.com/org/repo"})

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_reuses_already_classified_links_without_an_llm_call(self, openrouter_client):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(
                url="https://github.com/asl/BandageNG", is_relevant=True,
                llm_confidence=1.0, llm_reason="repository_archived_by_this_deposit")]),
        ])
        result = LinkRelevanceStage(prepared, raw).run()
        self.assertEqual(result["publications"], 1)
        openrouter_client.return_value.chat_json.assert_not_called()
        publication = next(prepared.read_models("publications", Publication))
        self.assertTrue(publication.has_code)
        self.assertEqual(json.loads(publication.code_url), ["https://github.com/asl/BandageNG"])

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_force_rejudges_llm_verdicts_but_not_the_archived_deposit(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": False, "confidence": 0.5, "reason": "re-judged",
        }
        openrouter_client.return_value.last_response = {"choices": []}
        openrouter_client.return_value.last_usage = None
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[
                CodeLink(url="https://github.com/asl/BandageNG", is_relevant=True,
                          llm_confidence=1.0, llm_reason="repository_archived_by_this_deposit"),
                CodeLink(url="https://github.com/org/repo", is_relevant=True,
                          llm_confidence=0.9, llm_reason="an earlier model's verdict"),
            ]),
        ])
        LinkRelevanceStage(prepared, raw, force=True).run()
        self.assertEqual(openrouter_client.return_value.chat_json.call_count, 1)
        links = {link.url: link for link in next(prepared.read_models("repo_links", RepoLink)).links}
        self.assertEqual(links["https://github.com/asl/BandageNG"].llm_reason,
                          "repository_archived_by_this_deposit")
        self.assertEqual(links["https://github.com/org/repo"].llm_reason, "re-judged")

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_marks_failed_when_the_llm_call_fails(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = None
        openrouter_client.return_value.last_response = None
        openrouter_client.return_value.last_usage = None
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(id="W1", title="paper")])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])
        LinkRelevanceStage(prepared, raw).run()
        rows = {r.id: r for r in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["link_relevance"].status, ProcessingStatus.FAILED)
        link = next(prepared.read_models("repo_links", RepoLink)).links[0]
        self.assertEqual(link.classification_status, ClassificationStatus.FAILED)
        self.assertIsNone(link.is_relevant)

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_force_failure_clears_the_stale_verdict_for_retry(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = None
        openrouter_client.return_value.last_response = None
        openrouter_client.return_value.last_usage = None
        openrouter_client.return_value.last_error = "temporary failure"
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [Publication(
            id="W1",
            title="paper",
            has_code=True,
            code_url='["https://github.com/org/repo"]',
        )])
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(
                url="https://github.com/org/repo",
                is_relevant=True,
                llm_confidence=0.9,
                llm_reason="old verdict",
            )]),
        ])

        LinkRelevanceStage(prepared, raw, force=True).run()

        link = next(prepared.read_models("repo_links", RepoLink)).links[0]
        self.assertEqual(link.classification_status, ClassificationStatus.FAILED)
        self.assertIsNone(link.is_relevant)
        self.assertIsNone(link.llm_confidence)
        self.assertIsNone(link.llm_reason)
        publication = next(prepared.read_models("publications", Publication))
        self.assertTrue(publication.has_code)
        self.assertEqual(publication.code_url, '["https://github.com/org/repo"]')

    def test_code_links_respects_publication_input_scope(self):
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="selected", abstract="https://github.com/org/repo"),
            Publication(id="W2", title="not selected", abstract="https://github.com/org/other"),
        ])
        CodeLinksStage(prepared, raw, selection=PreparedSelection("publications", frozenset({"W1"}))).run()
        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertIn("code_links", rows["W1"].processing)
        self.assertNotIn("code_links", rows["W2"].processing)

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_extracts_from_pdf_and_caches_the_download(self, http_client):
        pdf_bytes = _make_pdf_bytes([
            "Related work, nothing here.",
            "Our implementation: https://github.com/org/repo see the code.",
        ])
        http_client.return_value.get_bytes.return_value = pdf_bytes
        config = Settings(data_dir=self.root / "data", pdf_crawler_url="")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", pdf_url="https://example.org/w1.pdf"),
        ])
        CodeLinksStage(prepared, raw, config=config).run()

        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)
        [link] = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        self.assertEqual(link.url, "https://github.com/org/repo")
        self.assertEqual(len(link.occurrences), 1)
        self.assertEqual(link.occurrences[0].page_number, 2)
        assert link.occurrences[0].context is not None
        self.assertIn("github.com/org/repo", link.occurrences[0].context)
        self.assertEqual((config.pdf_dir / "W1.pdf").read_bytes(), pdf_bytes)
        self.assertIsNotNone(self.db.pdfs.find_one({"_id": "W1"}))

        # Cached on disk: a forced re-run must not download again.
        http_client.return_value.get_bytes.reset_mock()
        CodeLinksStage(prepared, raw, config=config, force=True).run()
        http_client.return_value.get_bytes.assert_not_called()

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_dedupes_per_page_and_orders_abstract_first(self, http_client):
        http_client.return_value.get_bytes.return_value = _make_pdf_bytes([
            "See https://github.com/org/repo and again https://github.com/org/repo here.",
            "Also https://github.com/org/repo on page two.",
        ])
        config = Settings(data_dir=self.root / "data")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", abstract="Code at https://github.com/org/repo.",
                        pdf_url="https://example.org/w1.pdf"),
        ])
        CodeLinksStage(prepared, raw, config=config).run()
        [link] = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        # abstract (None) first, then one occurrence per PDF page despite two
        # mentions on page 1 - repeats within the same page add no new info.
        self.assertEqual([o.page_number for o in link.occurrences], [None, 1, 2])

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_finds_github_url_only_reachable_via_hyperlink(self, http_client):
        pdf_bytes = _make_pdf_with_hyperlink("click here for the code", "https://github.com/org/repo")
        http_client.return_value.get_bytes.return_value = pdf_bytes
        config = Settings(data_dir=self.root / "data")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", pdf_url="https://example.org/w1.pdf"),
        ])
        CodeLinksStage(prepared, raw, config=config).run()
        [link] = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        self.assertEqual(link.url, "https://github.com/org/repo")
        self.assertEqual(link.occurrences[0].page_number, 1)
        # The URL itself is never rendered as text - only the annotation's
        # visible label ("click here...") is available as context.
        assert link.occurrences[0].context is not None
        self.assertIn("click here", link.occurrences[0].context)

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_prefers_visible_text_context_over_annotation(self, http_client):
        pdf_bytes = _make_pdf_with_hyperlink(
            "see also", "https://github.com/org/repo",
            page_text="Full implementation: https://github.com/org/repo is ours.",
        )
        http_client.return_value.get_bytes.return_value = pdf_bytes
        config = Settings(data_dir=self.root / "data")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", pdf_url="https://example.org/w1.pdf"),
        ])
        CodeLinksStage(prepared, raw, config=config).run()
        [link] = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        # One occurrence, not two - the annotation points at the same URL
        # already found in the visible text, so the richer text context wins.
        self.assertEqual(len(link.occurrences), 1)
        assert link.occurrences[0].context is not None
        self.assertIn("Full implementation", link.occurrences[0].context)

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_falls_back_to_abstract_when_pdf_download_fails(self, http_client):
        http_client.return_value.get_bytes.side_effect = RuntimeError("403 Client Error: Forbidden")
        config = Settings(data_dir=self.root / "data")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", abstract="https://github.com/org/repo",
                        pdf_url="https://example.org/w1.pdf"),
        ])
        CodeLinksStage(prepared, raw, config=config).run()

        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        state = rows["W1"].processing["code_links"]
        self.assertEqual(state.status, ProcessingStatus.FAILED)
        assert state.error is not None
        self.assertIn("403", state.error)
        links = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        self.assertEqual([link.url for link in links], ["https://github.com/org/repo"])
        self.assertFalse((config.pdf_dir / "W1.pdf").exists())
        self.assertIsNone(self.db.pdfs.find_one({"_id": "W1"}))

        # FAILED is retried on the next run even without --force (base.needs_attempt).
        http_client.return_value.get_bytes.side_effect = None
        http_client.return_value.get_bytes.return_value = _make_pdf_bytes(["ok, nothing here"])
        CodeLinksStage(prepared, raw, config=config).run()
        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_keeps_previous_pdf_evidence_when_a_retry_fails(self, http_client):
        http_client.return_value.get_bytes.return_value = _make_pdf_bytes([
            "Our code is available at https://github.com/org/repo",
        ])
        config = Settings(data_dir=self.root / "data")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", pdf_url="https://example.org/w1.pdf"),
        ])
        CodeLinksStage(prepared, raw, config=config).run()

        publications = list(prepared.read_models("publications", Publication))
        publications[0].processing["link_relevance"] = ProcessingState(
            status=ProcessingStatus.COMPLETED,
        )
        prepared.write_models("publications", publications)
        (config.pdf_dir / "W1.pdf").unlink()
        self.db.pdfs.delete_one({"_id": "W1"})
        http_client.return_value.get_bytes.side_effect = RuntimeError("temporary PDF failure")

        CodeLinksStage(prepared, raw, config=config, force=True).run()

        publication = next(prepared.read_models("publications", Publication))
        self.assertEqual(
            publication.processing["code_links"].status,
            ProcessingStatus.FAILED,
        )
        self.assertNotIn("link_relevance", publication.processing)
        [link] = next(prepared.read_models("repo_links", RepoLink)).links
        self.assertEqual(link.classification_status, ClassificationStatus.PENDING)
        self.assertEqual([occ.page_number for occ in link.occurrences], [1])
        context = link.occurrences[0].context
        self.assertIsNotNone(context)
        self.assertIn("Our code is available", context or "")

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_falls_back_to_crawler_when_no_pdf_url(self, http_client):
        pdf_bytes = _make_pdf_bytes(["From the crawler: https://github.com/org/repo"])

        def get_bytes(url, retries=3, timeout=None):
            self.assertTrue(url.startswith("http://crawler.local/api/v1/"))
            if url.endswith("/health"):
                return b"ok"
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["url"], ["https://doi.org/10.1234/abc"])
            return pdf_bytes

        http_client.return_value.get_bytes.side_effect = get_bytes
        config = Settings(data_dir=self.root / "data", pdf_crawler_url="http://crawler.local/api/v1")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            # pub.doi mirrors OpenAlex's `doi` field: already a full
            # https://doi.org/... URL, not a bare DOI.
            Publication(id="W1", title="t", doi="https://doi.org/10.1234/abc"),
        ])
        CodeLinksStage(prepared, raw, config=config).run()

        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)
        assert rows["W1"].full_text is not None
        self.assertEqual(rows["W1"].full_text.strip(), "From the crawler: https://github.com/org/repo")
        [link] = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
        self.assertEqual(link.url, "https://github.com/org/repo")

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_skips_crawler_when_unreachable_and_when_unconfigured(self, http_client):
        http_client.return_value.get_bytes.side_effect = RuntimeError("connection refused")
        prepared = PreparedStore(self.db, "sample")
        raw = RawStore(self.db, "sample")
        prepared.write_models("publications", [
            Publication(id="W1", title="t", doi="10.1234/abc"),
        ])

        # Not configured at all: health is never probed, download never attempted.
        CodeLinksStage(prepared, raw, config=Settings(data_dir=self.root / "data", pdf_crawler_url="")).run()
        http_client.return_value.get_bytes.assert_not_called()
        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)

        # Configured but unreachable: health probe fails closed, no FAILED status.
        config = Settings(data_dir=self.root / "data", pdf_crawler_url="http://crawler.local/api/v1")
        CodeLinksStage(prepared, raw, config=config, force=True).run()
        rows = {row.id: row for row in prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)
        self.assertIsNone(rows["W1"].full_text)


class HarvestAccountsTest(unittest.TestCase):
    """The people a repository is collected from, for the matcher to score."""

    OWNER_PAYLOAD = {
        "html_url": "https://github.com/org/repo", "name": "repo", "id": 1,
        "owner": {"login": "alice", "type": "User"},
    }

    def run_stage(self, github_client, *, contributors=(), commits=(), users=None):
        github_client.return_value.get_repository.return_value = self.OWNER_PAYLOAD
        github_client.return_value.has_readme.return_value = True
        github_client.return_value.contributors.return_value = list(contributors)
        github_client.return_value.commits.return_value = list(commits)
        github_client.return_value.get_user.side_effect = lambda login: (users or {}).get(login, {})
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        raw = RawStore(db, "sample")
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])
        RepositoriesStage(prepared, raw).run()
        RepoPeopleStage(prepared, raw).run()
        repos = list(prepared.read_models("repositories", Repository))
        profiles = {p.login: p for p in prepared.read_models("github_profiles", GitHubProfile)}
        return repos[0], profiles

    @staticmethod
    def commit(login, email, name):
        return {"author": {"login": login}, "commit": {"author": {"email": email, "name": name}}}

    def test_owner_and_contributors_become_candidates(self, ):
        repo, profiles = self.run_stage_wrapper(
            contributors=[{"login": "bob", "type": "User"}],
        )
        self.assertEqual(repo.contributors, ["alice", "bob"])
        self.assertEqual(sorted(profiles), ["alice", "bob"])
        self.assertEqual(profiles["bob"].repos, ["https://github.com/org/repo"])

    def test_commit_identities_land_on_the_account(self):
        repo, profiles = self.run_stage_wrapper(
            contributors=[{"login": "bob", "type": "User"}],
            commits=[self.commit("bob", "Bob@Example.com", "Bob Ivanov"),
                     self.commit("bob", "bob@itmo.ru", "Bob Ivanov")],
        )
        self.assertEqual(profiles["bob"].emails, ["bob@example.com", "bob@itmo.ru"])
        self.assertEqual(profiles["bob"].commit_names, ["Bob Ivanov"])

    def test_a_noreply_address_identifies_nobody(self):
        _, profiles = self.run_stage_wrapper(
            contributors=[{"login": "bob", "type": "User"}],
            commits=[self.commit("bob", "1234+bob@users.noreply.github.com", "Bob")],
        )
        self.assertEqual(profiles["bob"].emails, [])

    def test_bots_and_organizations_are_not_people(self):
        repo, profiles = self.run_stage_wrapper(
            contributors=[{"login": "dependabot[bot]", "type": "Bot"},
                          {"login": "some-org", "type": "Organization"},
                          {"login": "bob", "type": "User"}],
        )
        self.assertEqual(repo.contributors, ["alice", "bob"])

    def test_profile_fields_are_kept_for_the_matcher(self):
        _, profiles = self.run_stage_wrapper(
            contributors=[{"login": "bob", "type": "User"}],
            users={"bob": {"name": "Boris Ivanov", "company": "ITMO University",
                           "location": "Saint Petersburg", "bio": "researcher",
                           "email": "boris@itmo.ru"}},
        )
        profile = profiles["bob"]
        self.assertEqual((profile.name, profile.company, profile.location), (
            "Boris Ivanov", "ITMO University", "Saint Petersburg"))
        self.assertEqual(profile.emails, ["boris@itmo.ru"])

    def test_an_owner_of_two_repositories_keeps_what_both_revealed(self):
        # The owner profile is built from the repository payload, whose
        # nested owner carries only a login and a type. Writing it over the
        # profile instead of merging drops the emails, names and repository
        # list the same person left on every repository walked before.
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        raw = RawStore(db, "sample")
        prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[
                CodeLink(url="https://github.com/alice/first"),
                CodeLink(url="https://github.com/alice/second"),
            ]),
        ])
        with patch("pauk.pipeline.stages.repositories.GitHubClient") as client, \
                patch("pauk.pipeline.stages.repo_people.GitHubClient", client):
            client.return_value.get_repository.side_effect = lambda owner, name: {
                "html_url": f"https://github.com/{owner}/{name}", "name": name, "id": 1,
                "owner": {"login": "alice", "type": "User"}}
            client.return_value.has_readme.return_value = True
            client.return_value.contributors.return_value = []
            client.return_value.commits.side_effect = lambda owner, name, pages: [
                self.commit("alice", f"alice@{name}.org", "Alice Ivanova")]
            client.return_value.get_user.return_value = {"name": "Alice Ivanova"}
            RepositoriesStage(prepared, raw).run()
            RepoPeopleStage(prepared, raw).run()
        profile = {p.login: p for p in prepared.read_models("github_profiles", GitHubProfile)}["alice"]
        self.assertEqual(profile.emails, ["alice@first.org", "alice@second.org"])
        self.assertEqual(profile.repos, ["https://github.com/alice/first",
                                         "https://github.com/alice/second"])

    def test_a_failing_contributor_call_keeps_the_repository(self):
        # GitHub answers 403 on repositories it has not analysed. Since the
        # split that is a failure of repo_people alone: the metadata the
        # repositories stage already fetched keeps its completed status, and
        # the two halves record their state separately.
        with patch("pauk.pipeline.stages.repositories.GitHubClient") as client, \
                patch("pauk.pipeline.stages.repo_people.GitHubClient", client):
            client.return_value.contributors.side_effect = RuntimeError("403")
            repo, _profiles = self.run_stage(client)
        self.assertEqual(repo.github_id, 1)
        self.assertEqual(repo.contributors, [])
        self.assertEqual(repo.processing["repositories"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(repo.processing["repo_people"].status, ProcessingStatus.FAILED)

    def run_stage_wrapper(self, **kwargs):
        # Both stages build their own client; one mock stands in for both.
        with patch("pauk.pipeline.stages.repositories.GitHubClient") as client, \
                patch("pauk.pipeline.stages.repo_people.GitHubClient", client):
            return self.run_stage(client, **kwargs)


class AccountTypeSpellingTest(unittest.TestCase):
    """The account type reaches _is_person in two spellings.

    The stage passes what GitHub answered ("User", "Organization"); anything
    reading a stored GitHubProfile passes what the store keeps, which is
    lowercased. Comparing exactly made scripts/harvest_orphan_repos.py drop
    every owner whose profile was already in the database — silently, since
    a missing owner looks exactly like a repository nobody owns.
    """

    def test_both_spellings_of_a_user_are_a_person(self):
        self.assertTrue(_is_person("alice", "User"))
        self.assertTrue(_is_person("alice", "user"))

    def test_both_spellings_of_an_organization_are_not(self):
        self.assertFalse(_is_person("some-lab", "Organization"))
        self.assertFalse(_is_person("some-lab", "organization"))

    def test_an_unknown_type_is_still_taken_for_a_person(self):
        self.assertTrue(_is_person("alice", None))

    def test_a_bot_is_never_a_person(self):
        self.assertFalse(_is_person("dependabot[bot]", "user"))

    def test_the_owner_is_kept_when_the_type_came_from_a_stored_profile(self):
        # The call harvest_orphan_repos makes: the profile is already in the
        # database, so the type arrives lowercased.
        db = mongomock.MongoClient()["pauk_test"]
        stage = RepoPeopleStage(PreparedStore(db, "sample"), RawStore(db, "sample"))
        repo = Repository(id="github_alice_tool", name="tool",
                          url="https://github.com/alice/tool", owner_login="alice")
        client = Mock()
        client.contributors.return_value = []
        client.commits.return_value = []
        client.get_user.return_value = {"login": "alice", "type": "User"}
        # The type is read off the stored profile rather than passed in.
        profiles = {"github_alice": GitHubProfile(id="github_alice", login="alice",
                                                  type="user")}
        stage._harvest(client, repo, "alice", "tool", profiles)
        self.assertEqual(repo.contributors, ["alice"])


class OrganizationOwnerProfileTest(unittest.TestCase):
    """An organization owner gets a real profile, not just the owner stub."""

    URL = "https://github.com/some-lab/tool"

    def run_stage(self, github_client, owner_type, *, user_payload=None, repos=1):
        github_client.return_value.get_repository.side_effect = [
            {"html_url": f"https://github.com/some-lab/tool{i or ''}",
             "name": f"tool{i or ''}", "id": i + 1,
             "owner": {"login": "some-lab", "type": owner_type}}
            for i in range(repos)
        ]
        github_client.return_value.has_readme.return_value = True
        github_client.return_value.contributors.return_value = []
        github_client.return_value.commits.return_value = []
        github_client.return_value.get_user.return_value = user_payload or {}
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        prepared.write_models("repo_links", [
            RepoLink(publication_id=f"W{i}", links=[
                CodeLink(url=f"https://github.com/some-lab/tool{i or ''}")])
            for i in range(repos)
        ])
        RepositoriesStage(prepared, RawStore(db, "sample")).run()
        return github_client.return_value, {
            p.login: p for p in prepared.read_models("github_profiles", GitHubProfile)}

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_an_organization_profile_is_filled_in(self, github_client):
        # The nested owner object carries no name or location, and the people
        # stage skips organizations, so without this the fields social_graph
        # reads would never be populated.
        _, profiles = self.run_stage(github_client, "Organization", user_payload={
            "name": "Some Lab", "description": "a lab at ITMO University",
            "location": "Saint Petersburg", "type": "Organization"})
        self.assertEqual(profiles["some-lab"].name, "Some Lab")
        self.assertEqual(profiles["some-lab"].description, "a lab at ITMO University")
        self.assertEqual(profiles["some-lab"].location, "Saint Petersburg")

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_the_organization_is_fetched_once_however_many_repositories(self, github_client):
        client, _ = self.run_stage(github_client, "Organization", repos=3,
                                   user_payload={"name": "Some Lab"})
        self.assertEqual(client.get_user.call_count, 1)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_personal_owner_is_left_to_the_harvest(self, github_client):
        # Only organizations are fetched here. A user owning the repository is
        # a contributor candidate, and RepoPeopleStage fetches them with
        # everyone else — this stage does not call get_user for them at all.
        client, _ = self.run_stage(github_client, "User")
        self.assertEqual([call.args for call in client.get_user.call_args_list], [])


class OwnerProfileIsFetchedTest(unittest.TestCase):
    """The owner stub must not pass for a fetched profile.

    `repositories` writes a GitHubProfile for the owner out of the nested
    owner object, which carries a login, a URL and a type. `repo_people` then
    decides whether GET /users/{login} is still worth a call. Deciding that on
    `html_url` meant the stub answered for the real profile, and no repository
    owner was ever fetched — the one person most likely to be an ITMO author.
    """

    PAYLOAD = {"html_url": "https://github.com/alice/tool", "name": "tool", "id": 1,
               "owner": {"login": "alice", "type": "User",
                         "html_url": "https://github.com/alice"}}
    USER = {"login": "alice", "type": "User", "name": "Alice Ivanova",
            "email": "alice@itmo.ru", "location": "Saint Petersburg"}

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1",
                     links=[CodeLink(url="https://github.com/alice/tool")])])

    def _run_both_stages(self):
        with patch("pauk.pipeline.stages.repositories.GitHubClient") as client:
            client.return_value.get_repository.return_value = self.PAYLOAD
            client.return_value.has_readme.return_value = True
            client.return_value.get_user.return_value = self.USER
            RepositoriesStage(self.prepared, self.raw).run()
        with patch("pauk.pipeline.stages.repo_people.GitHubClient") as client:
            client.return_value.contributors.return_value = [
                {"login": "alice", "type": "User"}]
            client.return_value.commits.return_value = []
            client.return_value.get_user.return_value = self.USER
            RepoPeopleStage(self.prepared, self.raw).run()
            calls = client.return_value.get_user.call_count
        return calls, {p.login: p
                       for p in self.prepared.read_models("github_profiles", GitHubProfile)}

    def test_the_owner_behind_a_stub_is_still_fetched(self):
        calls, profiles = self._run_both_stages()
        self.assertEqual(calls, 1)
        self.assertEqual(profiles["alice"].name, "Alice Ivanova")
        self.assertEqual(profiles["alice"].location, "Saint Petersburg")
        self.assertIn("alice@itmo.ru", profiles["alice"].emails)

    def test_a_fetched_profile_is_not_fetched_again(self):
        self._run_both_stages()
        # The marker is what the second run reads; the point of the gate is
        # that a known account costs no call at all.
        calls, profiles = self._run_both_stages()
        self.assertEqual(calls, 0)
        self.assertTrue(profiles["alice"].profile_fetched)

    def test_a_profile_stored_before_the_marker_counts_as_fetched(self):
        # Written by the pipeline that always called the endpoint. Re-fetching
        # every such profile once would cost an hour of GitHub's quota.
        self.prepared.write_models("github_profiles", [
            GitHubProfile(id="github_alice", login="alice", name="Alice Ivanova",
                          html_url="https://github.com/alice", type="user")])
        calls, _ = self._run_both_stages()
        self.assertEqual(calls, 0)

    def test_a_failed_fetch_leaves_the_account_open_for_another_attempt(self):
        # GitHub answering 502 is not evidence about the account, so the
        # marker must stay down. Whether the repository is revisited at all is
        # the stage's own `needs_attempt` question, which a completed row
        # answers no — so the retry is observed on the next visit it does make.
        with patch("pauk.pipeline.stages.repositories.GitHubClient") as client:
            client.return_value.get_repository.return_value = self.PAYLOAD
            client.return_value.has_readme.return_value = True
            client.return_value.get_user.return_value = {}
            RepositoriesStage(self.prepared, self.raw).run()
        with patch("pauk.pipeline.stages.repo_people.GitHubClient") as client:
            client.return_value.contributors.return_value = [
                {"login": "alice", "type": "User"}]
            client.return_value.commits.return_value = []
            client.return_value.get_user.side_effect = RuntimeError("502")
            RepoPeopleStage(self.prepared, self.raw).run()
        profiles = {p.login: p
                    for p in self.prepared.read_models("github_profiles", GitHubProfile)}
        self.assertFalse(profiles["alice"].profile_fetched)

        with patch("pauk.pipeline.stages.repo_people.GitHubClient") as client:
            client.return_value.contributors.return_value = [
                {"login": "alice", "type": "User"}]
            client.return_value.commits.return_value = []
            client.return_value.get_user.return_value = self.USER
            RepoPeopleStage(self.prepared, self.raw, force=True).run()
        profiles = {p.login: p
                    for p in self.prepared.read_models("github_profiles", GitHubProfile)}
        self.assertTrue(profiles["alice"].profile_fetched)
        self.assertEqual(profiles["alice"].name, "Alice Ivanova")


class CanonicalRekeyAliasTest(unittest.TestCase):
    """Re-keying a row to its canonical id must leave the old id behind.

    `merged_ids` is the alias table the graph loader re-folds edges through
    (`graph/jsonl_loader.py`). An id dropped here is an edge that never
    reaches the surviving node.
    """

    CANON = {"html_url": "https://github.com/alice/tool", "name": "tool", "id": 1,
             "owner": {"login": "alice", "type": "User"}}

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")

    def _run(self, rows):
        self.prepared.write_models("repositories", rows)
        with patch("pauk.pipeline.stages.repositories.GitHubClient") as client:
            client.return_value.get_repository.return_value = self.CANON
            client.return_value.has_readme.return_value = True
            client.return_value.get_user.return_value = {}
            RepositoriesStage(self.prepared, self.raw).run()
        return list(self.prepared.read_models("repositories", Repository))

    def test_a_renamed_row_keeps_its_old_id(self):
        # No duplicate involved: GitHub redirects the old name, so the single
        # stored row is re-keyed and its id would otherwise be overwritten.
        rows = self._run([Repository(id="github_alice_oldname", name="oldname",
                                     url="https://github.com/alice/oldname")])
        self.assertEqual([row.id for row in rows], ["github_alice_tool"])
        self.assertEqual(rows[0].merged_ids, ["github_alice_oldname"])

    def test_a_row_folded_by_canonical_id_leaves_every_alias_behind(self):
        rows = self._run([
            Repository(id="github_alice_oldname", name="oldname",
                       url="https://github.com/alice/oldname",
                       merged_ids=["github_alice_ancient"], publication_ids=["W1"]),
            Repository(id="github_alice_older", name="older",
                       url="https://github.com/alice/older",
                       merged_ids=["github_alice_prehistoric"], publication_ids=["W2"]),
        ])
        self.assertEqual([row.id for row in rows], ["github_alice_tool"])
        self.assertEqual(sorted(rows[0].merged_ids), [
            "github_alice_ancient", "github_alice_older",
            "github_alice_oldname", "github_alice_prehistoric"])
        self.assertEqual(sorted(rows[0].publication_ids), ["W1", "W2"])

    def test_the_canonical_id_is_never_its_own_alias(self):
        rows = self._run([
            Repository(id="github_alice_tool", name="tool",
                       url="https://github.com/alice/tool"),
            Repository(id="github_alice_oldname", name="oldname",
                       url="https://github.com/alice/oldname",
                       merged_ids=["github_alice_tool"]),
        ])
        self.assertEqual([row.id for row in rows], ["github_alice_tool"])
        self.assertNotIn("github_alice_tool", rows[0].merged_ids)
        self.assertIn("github_alice_oldname", rows[0].merged_ids)


class ImplementsFromRelevanceTest(unittest.TestCase):
    """publication_ids, and so the IMPLEMENTS edge, follows link_relevance."""

    # The owner here must match the URL's: the stage re-keys each row to
    # github_{owner}_{name} taken from the fetched payload.
    PAYLOAD = {"html_url": "https://github.com/org/repo", "name": "repo", "id": 1,
               "owner": {"login": "org", "type": "Organization"}}
    REPO_ID = "github_org_repo"
    URL = "https://github.com/org/repo"

    def run_stage(self, github_client, rows):
        github_client.return_value.get_repository.return_value = self.PAYLOAD
        github_client.return_value.has_readme.return_value = True
        github_client.return_value.contributors.return_value = []
        github_client.return_value.commits.return_value = []
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        prepared.write_models("repo_links", rows)
        RepositoriesStage(prepared, RawStore(db, "sample")).run()
        return {repo.id: repo for repo in prepared.read_models("repositories", Repository)}

    def link(self, publication, is_relevant):
        return RepoLink(publication_id=publication,
                        links=[CodeLink(url=self.URL, is_relevant=is_relevant)])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_tool_the_paper_merely_cites_is_not_implemented(self, github_client):
        repos = self.run_stage(github_client, [self.link("W1", False)])
        self.assertEqual(repos[self.REPO_ID].publication_ids, [])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_the_citation_survives_even_when_the_claim_does_not(self, github_client):
        repos = self.run_stage(github_client, [self.link("W1", False)])
        self.assertEqual(repos[self.REPO_ID].cited_urls, [self.URL])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_the_authors_own_code_is_implemented(self, github_client):
        repos = self.run_stage(github_client, [self.link("W1", True)])
        self.assertEqual(repos[self.REPO_ID].publication_ids, ["W1"])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_an_unjudged_link_is_not_implemented(self, github_client):
        repos = self.run_stage(github_client, [self.link("W1", None)])
        self.assertEqual(repos[self.REPO_ID].publication_ids, [])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_classified_uncertain_link_is_only_a_citation(self, github_client):
        row = RepoLink(publication_id="W1", links=[CodeLink(
            url=self.URL,
            classification_status=ClassificationStatus.CLASSIFIED,
            is_relevant=None,
            llm_confidence=0.3,
            llm_reason="insufficient context",
        )])

        repository = self.run_stage(github_client, [row])[self.REPO_ID]

        self.assertEqual(repository.publication_ids, [])
        self.assertEqual(repository.cited_urls, [self.URL])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_only_the_paper_whose_code_it_is_makes_a_claim(self, github_client):
        repos = self.run_stage(github_client, [self.link("W1", False), self.link("W2", True)])
        self.assertEqual(repos[self.REPO_ID].publication_ids, ["W2"])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_one_relevant_link_is_enough_within_a_publication(self, github_client):
        rows = [RepoLink(publication_id="W1", links=[
            CodeLink(url=self.URL, is_relevant=False),
            CodeLink(url=self.URL, is_relevant=True),
        ])]
        repos = self.run_stage(github_client, rows)
        self.assertEqual(repos[self.REPO_ID].publication_ids, ["W1"])


class CollectOccurrencesTest(unittest.TestCase):
    def test_dedupes_within_one_text_keeps_first_context(self):
        text = "first https://github.com/org/repo then https://github.com/org/repo again"
        found = _occurrences_in_text(text, page_number=5)
        self.assertEqual(list(found), ["https://github.com/org/repo"])
        self.assertEqual(found["https://github.com/org/repo"].page_number, 5)

    def test_collect_merges_abstract_and_pages_in_order(self):
        # pdf_page_occurrences is a list of per-page dicts (what _extract_pdf
        # would hand back), not raw page text - build them the same way.
        occurrences = _collect_occurrences(
            "https://github.com/org/repo",
            [
                _occurrences_in_text("nothing here", 1),
                _occurrences_in_text("https://github.com/org/repo again", 2),
                _occurrences_in_text("https://github.com/other/x", 3),
            ],
        )
        self.assertEqual([o.page_number for o in occurrences["https://github.com/org/repo"]], [None, 2])
        self.assertEqual([o.page_number for o in occurrences["https://github.com/other/x"]], [3])


class GithubUrlRegexTest(unittest.TestCase):
    def test_matches_bare_domain_without_scheme(self):
        found = _occurrences_in_text("code at github.com/org/repo, see paper", None)
        self.assertEqual(list(found), ["https://github.com/org/repo"])

    def test_matches_bare_www_without_scheme(self):
        found = _occurrences_in_text("mirror: www.github.com/org/repo", None)
        self.assertEqual(list(found), ["https://github.com/org/repo"])

    def test_does_not_match_inside_a_longer_hostname(self):
        # "github.com" glued onto a preceding word/dot isn't a github.com host.
        found = _occurrences_in_text("see mygithub.com/org/repo and sub.github.com/org/repo", None)
        self.assertEqual(found, {})

    def test_truncates_a_deep_path_to_owner_repo(self):
        found = _occurrences_in_text("full path: https://github.com/org/repo/blob/main/README.md", None)
        self.assertEqual(list(found), ["https://github.com/org/repo"])

    def test_strips_assorted_trailing_punctuation(self):
        found = _occurrences_in_text("(see https://github.com/org/repo), it works!", None)
        self.assertEqual(list(found), ["https://github.com/org/repo"])

    def test_rejoins_a_repo_name_split_by_a_hyphenated_line_wrap(self):
        text = "https://github.com/org/detec-\ntron2 rocks"
        found = _occurrences_in_text(text, None)
        self.assertEqual(list(found), ["https://github.com/org/detectron2"])

    def test_rejoins_an_owner_name_split_by_a_hyphenated_line_wrap(self):
        text = "https://github.com/facebook-\nresearch/detectron2 is great"
        found = _occurrences_in_text(text, None)
        self.assertEqual(list(found), ["https://github.com/facebookresearch/detectron2"])

    def test_does_not_glue_the_next_sentence_onto_a_url_at_a_plain_line_break(self):
        # No hyphen at the break: nothing distinguishes a mid-URL wrap from an
        # ordinary sentence boundary, so this must NOT extend into "Not a link".
        text = "our code is at github.com/org/repo.\nNot a link: mygithub.com/should/not/match"
        found = _occurrences_in_text(text, None)
        self.assertEqual(list(found), ["https://github.com/org/repo"])

    def test_strips_a_sentence_glued_with_no_separator_at_all(self):
        # No newline anywhere here - a PDF kerning/footnote artifact renders
        # the next sentence with zero gap after the URL.
        found = _occurrences_in_text("Available at https://github.com/org/repo.We evaluate it next.", None)
        self.assertEqual(list(found), ["https://github.com/org/repo"])

    def test_strips_a_footnote_number_glued_after_a_period(self):
        found = _occurrences_in_text("Code: https://github.com/org/repo.12 citations so far.", None)
        self.assertEqual(list(found), ["https://github.com/org/repo"])

    def test_keeps_a_bare_trailing_digit_with_no_period(self):
        # Unsolvable ambiguity, same as the old script: a GitHub repo name can
        # genuinely end in a digit (detectron2), so this is left alone.
        found = _occurrences_in_text("Our tool https://github.com/org/repo1 does the job.", None)
        self.assertEqual(list(found), ["https://github.com/org/repo1"])

    def test_does_not_catch_a_url_wrapped_with_no_hyphen(self):
        # The accepted trade-off: safer than gluing unrelated text onto a match.
        found = _occurrences_in_text("code at https://github.com/org/\nrepo for details", None)
        self.assertEqual(found, {})


class NormalizeLigaturesTest(unittest.TestCase):
    def test_decomposes_common_ligatures(self):
        self.assertEqual(_normalize_ligatures("caﬀe"), "caffe")
        self.assertEqual(_normalize_ligatures("eﬃcient"), "efficient")

    def test_two_ligature_variants_of_the_same_repo_collapse_to_one_url(self):
        # Real bug, found on an actual paper (SSD, arXiv:1512.02325): PDF fonts
        # render "ff" as one glyph (U+FB00), which \w matches as a letter, so
        # "caﬀe" and "caffe" used to become two different repos.
        text = "See https://github.com/weiliu89/caﬀe and also https://github.com/weiliu89/caffe."
        found = _occurrences_in_text(_normalize_ligatures(text), None)
        self.assertEqual(list(found), ["https://github.com/weiliu89/caffe"])


class UnlinkedRepositoriesTest(unittest.TestCase):
    """Rows that arrived without a repo_links line behind them.

    A curated import writes the Repository straight into the collection, so a
    work list built only from repo_links can never reach it again.
    """

    PAYLOAD = {
        "html_url": "https://github.com/org/curated", "name": "curated", "id": 7,
        "owner": {"login": "org", "type": "Organization"}, "language": "Python",
        "topics": ["ml"], "stargazers_count": 3,
    }

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")

    def _client(self, github_client):
        github_client.return_value.get_repository.return_value = self.PAYLOAD
        github_client.return_value.has_readme.return_value = True
        github_client.return_value.contributors.return_value = []
        github_client.return_value.commits.return_value = []
        # The owner in PAYLOAD is an organization, which the stage now fetches.
        github_client.return_value.get_user.return_value = {}
        return github_client

    def _row(self):
        return {row.id: row for row in self.prepared.read_models("repositories", Repository)}

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_row_without_a_link_is_still_enriched(self, github_client):
        self._client(github_client)
        self.prepared.write_models("repositories", [
            Repository(id="github_org_curated", name="curated",
                       url="https://github.com/org/curated"),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        row = self._row()["github_org_curated"]
        self.assertEqual(row.language, "Python")
        self.assertEqual(row.topics, ["ml"])
        self.assertEqual(row.processing["repositories"].status, ProcessingStatus.COMPLETED)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_completed_row_is_left_alone_until_forced(self, github_client):
        self._client(github_client)
        self.prepared.write_models("repositories", [
            Repository(id="github_org_curated", name="curated",
                       url="https://github.com/org/curated"),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)

        RepositoriesStage(self.prepared, self.raw).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)

        RepositoriesStage(self.prepared, self.raw, force=True).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 2)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_linked_row_is_not_fetched_twice_by_the_second_pass(self, github_client):
        self._client(github_client)
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1",
                     links=[CodeLink(url="https://github.com/org/curated")]),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_row_whose_url_is_not_a_github_repository_is_skipped(self, github_client):
        self._client(github_client)
        self.prepared.write_models("repositories", [
            Repository(id="gitlab_org_thing", name="thing",
                       url="https://gitlab.com/org/thing"),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 0)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_row_whose_url_was_rewritten_is_not_fetched_twice(self, github_client):
        """A rename redirects the fetch, and the row keeps the canonical URL.

        The failure comes later, so the row still needs an attempt — and the
        second pass, keyed by `repo.url`, must recognise it as one already
        made instead of spending another call on the same repository.
        """
        self._client(github_client)
        github_client.return_value.get_repository.return_value = {
            **self.PAYLOAD, "html_url": "https://github.com/org/renamed", "name": "renamed",
        }
        github_client.return_value.has_readme.side_effect = RuntimeError("502")
        self.prepared.write_models("repositories", [
            Repository(id="github_org_curated", name="curated",
                       url="https://github.com/org/curated"),
        ])
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1",
                     links=[CodeLink(url="https://github.com/org/curated")]),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_two_rows_for_one_url_are_folded_rather_than_dropped(self, github_client):
        """Same repository under two ids — a curated import and a link pass.

        Keying the second pass by URL collapses them onto one key. The loser
        must not simply vanish from the work list: rows are read in a stable
        order, so it would lose on every run and never be enriched at all.
        """
        self._client(github_client)
        self.prepared.write_models("repositories", [
            Repository(id="curated_1", name="curated", publication_ids=["W1"],
                       url="https://github.com/org/curated"),
            Repository(id="curated_2", name="curated", publication_ids=["W2"],
                       cited_urls=["https://github.com/org/Curated"],
                       url="https://github.com/org/curated"),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)
        rows = self._row()
        self.assertEqual(list(rows), ["github_org_curated"])
        row = rows["github_org_curated"]
        self.assertEqual(row.processing["repositories"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(sorted(row.publication_ids), ["W1", "W2"])
        # Both stored ids survive as aliases: curated_2 lost the fold, and
        # curated_1 won it but was then re-keyed to the canonical id. Either
        # one can still be what a published edge points at.
        self.assertEqual(sorted(row.merged_ids), ["curated_1", "curated_2"])
        self.assertIn("https://github.com/org/Curated", row.cited_urls)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_folded_row_never_lists_the_id_it_ends_up_with(self, github_client):
        """The loser can be keyed by the very id canonicalization then hands
        the winner; a row listing itself as merged away would confuse the
        graph loader's alias table."""
        self._client(github_client)
        self.prepared.write_models("repositories", [
            Repository(id="curated_1", name="curated",
                       url="https://github.com/org/curated"),
            Repository(id="github_org_curated", name="curated",
                       url="https://github.com/org/curated"),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        row = self._row()["github_org_curated"]
        self.assertNotIn("github_org_curated", row.merged_ids)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_the_row_that_already_reached_the_api_wins_the_fold(self, github_client):
        """github_id is only ever set from a payload, so the row carrying one
        is the row whose name and URL are canonical."""
        self._client(github_client)
        self.prepared.write_models("repositories", [
            Repository(id="aaa_first_by_id", name="curated",
                       url="https://github.com/org/curated"),
            Repository(id="zzz_last_by_id", name="curated", github_id=7,
                       url="https://github.com/org/curated"),
        ])
        RepositoriesStage(self.prepared, self.raw, force=True).run()
        rows = self._row()
        self.assertEqual(list(rows), ["github_org_curated"])
        self.assertIn("aaa_first_by_id", rows["github_org_curated"].merged_ids)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_the_attempt_history_survives_the_fold(self, github_client):
        """With no payload on either row, the one that has already been tried
        wins — folding it away would reset the attempt counter."""
        self._client(github_client)
        github_client.return_value.get_repository.side_effect = RuntimeError("404")
        tried = Repository(id="zzz_last_by_id", name="curated",
                           url="https://github.com/org/curated")
        tried.processing["repositories"] = ProcessingState(
            status=ProcessingStatus.FAILED, attempts=2)
        self.prepared.write_models("repositories", [
            Repository(id="aaa_first_by_id", name="curated",
                       url="https://github.com/org/curated"),
            tried,
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        rows = self._row()
        self.assertEqual(list(rows), ["zzz_last_by_id"])
        self.assertEqual(rows["zzz_last_by_id"].processing["repositories"].attempts, 3)
        self.assertIn("aaa_first_by_id", rows["zzz_last_by_id"].merged_ids)

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_an_id_scoped_run_skips_rows_it_does_not_name(self, github_client):
        self._client(github_client)
        self.prepared.write_models("repositories", [
            Repository(id="github_org_curated", name="curated",
                       url="https://github.com/org/curated"),
            Repository(id="github_org_other", name="other",
                       url="https://github.com/org/other"),
        ])
        selection = PreparedSelection(entity="repositories", ids={"github_org_curated"})
        RepositoriesStage(self.prepared, self.raw, selection=selection).run()
        self.assertEqual(github_client.return_value.get_repository.call_count, 1)


class RepoPeopleStageTest(unittest.TestCase):
    """Metadata and people are two stages, so each can go stale on its own."""

    PAYLOAD = {
        "html_url": "https://github.com/org/repo", "name": "repo", "id": 1,
        "owner": {"login": "alice", "type": "User"},
    }

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])

    def _client(self, client, *, users=None):
        client.return_value.get_repository.return_value = self.PAYLOAD
        client.return_value.has_readme.return_value = True
        client.return_value.contributors.return_value = [{"login": "bob", "type": "User"}]
        client.return_value.commits.return_value = []
        client.return_value.get_user.side_effect = lambda login: (users or {}).get(login, {})
        return client

    def _profiles(self):
        return {p.login: p for p in self.prepared.read_models("github_profiles", GitHubProfile)}

    def _repo(self):
        return list(self.prepared.read_models("repositories", Repository))[0]

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_the_metadata_stage_no_longer_touches_people(self, client):
        self._client(client)
        RepositoriesStage(self.prepared, self.raw).run()
        self.assertEqual(client.return_value.get_repository.call_count, 1)
        self.assertEqual(client.return_value.contributors.call_count, 0)
        self.assertEqual(client.return_value.commits.call_count, 0)
        self.assertEqual(client.return_value.get_user.call_count, 0)
        self.assertEqual(self._repo().contributors, [])

    @patch("pauk.pipeline.stages.repo_people.GitHubClient")
    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_each_half_records_its_own_state(self, repos_client, people_client):
        self._client(repos_client)
        self._client(people_client)
        RepositoriesStage(self.prepared, self.raw).run()
        RepoPeopleStage(self.prepared, self.raw).run()
        repo = self._repo()
        self.assertEqual(repo.processing["repositories"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(repo.processing["repo_people"].status, ProcessingStatus.COMPLETED)
        # The owner's type round-tripped through the stored profile, which
        # lowercased it — he still counts as a person.
        self.assertEqual(repo.contributors, ["alice", "bob"])

    @patch("pauk.pipeline.stages.repo_people.GitHubClient")
    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_forcing_the_metadata_stage_does_not_re_harvest_people(self, repos_client, people_client):
        self._client(repos_client)
        self._client(people_client)
        RepositoriesStage(self.prepared, self.raw).run()
        RepoPeopleStage(self.prepared, self.raw).run()
        before = people_client.return_value.contributors.call_count

        RepositoriesStage(self.prepared, self.raw, force=True).run()
        self.assertEqual(repos_client.return_value.get_repository.call_count, 2)
        self.assertEqual(people_client.return_value.contributors.call_count, before)

    @patch("pauk.pipeline.stages.repo_people.GitHubClient")
    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_filled_profile_is_not_fetched_again(self, repos_client, people_client):
        users = {"alice": {"html_url": "https://github.com/alice", "name": "Alice"},
                 "bob": {"html_url": "https://github.com/bob", "name": "Bob"}}
        self._client(repos_client, users=users)
        self._client(people_client, users=users)
        RepositoriesStage(self.prepared, self.raw).run()
        RepoPeopleStage(self.prepared, self.raw).run()
        self.assertEqual(people_client.return_value.get_user.call_count, 2)
        self.assertEqual(self._profiles()["bob"].name, "Bob")

        RepoPeopleStage(self.prepared, self.raw, force=True).run()
        # Forced: the profiles are re-read, but only because they were asked for.
        self.assertEqual(people_client.return_value.get_user.call_count, 4)

    @patch("pauk.pipeline.stages.repo_people.GitHubClient")
    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_second_repository_reuses_the_profile_it_already_has(self, repos_client, people_client):
        users = {"bob": {"html_url": "https://github.com/bob", "name": "Bob"}}
        self._client(repos_client, users=users)
        self._client(people_client, users=users)
        repos_client.return_value.get_repository.side_effect = lambda owner, name: {
            "html_url": f"https://github.com/{owner}/{name}", "name": name, "id": 1,
            "owner": {"login": "org", "type": "Organization"}}
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[
                CodeLink(url="https://github.com/org/first"),
                CodeLink(url="https://github.com/org/second"),
            ]),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        RepoPeopleStage(self.prepared, self.raw).run()
        # bob is credited on both repositories; his profile is fetched once.
        self.assertEqual(people_client.return_value.contributors.call_count, 2)
        self.assertEqual(people_client.return_value.get_user.call_count, 1)
        self.assertEqual(sorted(self._profiles()["bob"].repos),
                         ["https://github.com/org/first", "https://github.com/org/second"])

    @patch("pauk.pipeline.stages.repo_people.GitHubClient")
    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_a_publication_scoped_run_leaves_other_papers_repositories_alone(
            self, repos_client, people_client):
        """`--input pubs.txt --entity publications` means those publications.

        `in_scope` alone answers True for every repository when the selection
        names publications, so without a scope of its own this stage would
        walk the whole group and spend the GitHub quota on repositories
        nobody asked about.
        """
        users = {"bob": {"html_url": "https://github.com/bob", "name": "Bob"}}
        self._client(repos_client, users=users)
        self._client(people_client, users=users)
        repos_client.return_value.get_repository.side_effect = lambda owner, name: {
            "html_url": f"https://github.com/{owner}/{name}", "name": name, "id": 1,
            "owner": {"login": "org", "type": "Organization"}}
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[
                CodeLink(url="https://github.com/org/first", is_relevant=True),
            ]),
            RepoLink(publication_id="W2", links=[
                CodeLink(url="https://github.com/org/second", is_relevant=True),
            ]),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        selection = PreparedSelection(entity="publications", ids=frozenset({"W1"}))
        RepoPeopleStage(self.prepared, self.raw, selection=selection).run()
        self.assertEqual(people_client.return_value.contributors.call_count, 1)
        people_client.return_value.contributors.assert_called_once_with("org", "first")

    @patch("pauk.pipeline.stages.repo_people.GitHubClient")
    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_an_id_scoped_run_still_filters_by_repository(self, repos_client, people_client):
        """A selection aimed at repositories keeps working as it did."""
        self._client(repos_client, users={})
        self._client(people_client, users={})
        repos_client.return_value.get_repository.side_effect = lambda owner, name: {
            "html_url": f"https://github.com/{owner}/{name}", "name": name, "id": 1,
            "owner": {"login": "org", "type": "Organization"}}
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[
                CodeLink(url="https://github.com/org/first"),
                CodeLink(url="https://github.com/org/second"),
            ]),
        ])
        RepositoriesStage(self.prepared, self.raw).run()
        selection = PreparedSelection(entity="repositories",
                                      ids=frozenset({"github_org_second"}))
        RepoPeopleStage(self.prepared, self.raw, selection=selection).run()
        people_client.return_value.contributors.assert_called_once_with("org", "second")


if __name__ == "__main__":
    unittest.main()
