import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import fitz

from pauk.models import CodeLink, Publication, RepoLink
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.code_links import (
    CodeLinksStage,
    _collect_occurrences,
    _normalize_ligatures,
    _occurrences_in_text,
)
from pauk.pipeline.stages.link_relevance import LinkRelevanceStage
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
    def test_code_links_marks_empty_and_found_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="with code", abstract="https://github.com/org/repo"),
                Publication(id="W2", title="without code"),
            ])
            CodeLinksStage(prepared, raw).run()
            rows = {row.id: row for row in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)
            self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)
            self.assertTrue(rows["W1"].has_code)
            links = {r.publication_id: r for r in prepared.read_models("repo_links", RepoLink)}
            # code_links only records what was found; whether it's the
            # authors' own artifact is link_relevance's call, not this stage's.
            self.assertIsNone(links["W1"].links[0].is_relevant)

    def test_code_links_strips_sentence_ending_period_from_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="t", abstract="Code is available at https://github.com/org/repo."),
            ])
            CodeLinksStage(prepared, raw).run()
            links = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
            self.assertEqual([link.url for link in links], ["https://github.com/org/repo"])

    def test_code_links_canonicalizes_www_github_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("repo_links", [
                RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
                RepoLink(publication_id="W2", links=[CodeLink(url="https://www.github.com/org/repo")]),
            ])
            RepositoriesStage(prepared, raw, force=True).run()
            self.assertEqual(github_client.return_value.get_repository.call_count, 1)

    def test_force_reprocesses_completed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="asl/BandageNG: Continuous build", type="software"),
                Publication(id="W2", title="A/B testing: what we measured", type="dataset"),
                Publication(id="W3", title="ablab/spades: Release v4.3.0", type="article"),
            ])
            CodeLinksStage(prepared, raw).run()
            rows = {r.id: r for r in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].code_url, "https://github.com/asl/BandageNG")
            links = {r.publication_id: r for r in prepared.read_models("repo_links", RepoLink)}
            self.assertEqual(links["W1"].links[0].llm_reason, "repository_archived_by_this_deposit")
            # A title with a space before the colon is prose, not owner/name,
            # and a plain article is never read as an archive.
            self.assertIsNone(rows["W2"].code_url)
            self.assertIsNone(rows["W3"].code_url)

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_classifies_pending_links(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": True, "confidence": 0.9, "reason": "authors say so",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [Publication(id="W1", title="paper")])
            prepared.write_models("repo_links", [
                RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
            ])
            LinkRelevanceStage(prepared, raw).run()
            rows = {r.id: r for r in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["link_relevance"].status, ProcessingStatus.COMPLETED)
            link = next(prepared.read_models("repo_links", RepoLink)).links[0]
            self.assertTrue(link.is_relevant)
            self.assertEqual(link.llm_confidence, 0.9)
            self.assertEqual(link.llm_reason, "authors say so")

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_skips_already_classified_links(self, openrouter_client):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [Publication(id="W1", title="paper")])
            prepared.write_models("repo_links", [
                RepoLink(publication_id="W1", links=[CodeLink(
                    url="https://github.com/asl/BandageNG", is_relevant=True,
                    llm_confidence=1.0, llm_reason="repository_archived_by_this_deposit")]),
            ])
            result = LinkRelevanceStage(prepared, raw).run()
            self.assertEqual(result["publications"], 0)
            openrouter_client.return_value.chat_json.assert_not_called()

    @patch("pauk.pipeline.stages.link_relevance.OpenRouterClient")
    def test_link_relevance_force_rejudges_llm_verdicts_but_not_the_archived_deposit(self, openrouter_client):
        openrouter_client.return_value.chat_json.return_value = {
            "is_authors_artifact": False, "confidence": 0.5, "reason": "re-judged",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [Publication(id="W1", title="paper")])
            prepared.write_models("repo_links", [
                RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
            ])
            LinkRelevanceStage(prepared, raw).run()
            rows = {r.id: r for r in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["link_relevance"].status, ProcessingStatus.FAILED)
            link = next(prepared.read_models("repo_links", RepoLink)).links[0]
            self.assertIsNone(link.is_relevant)

    def test_code_links_respects_publication_input_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Settings(data_dir=root / "data")
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
            self.assertTrue((config.pdf_dir / "sample" / "W1.pdf").is_file())

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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Settings(data_dir=root / "data")
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Settings(data_dir=root / "data")
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Settings(data_dir=root / "data")
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Settings(data_dir=root / "data")
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
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
            self.assertFalse((config.pdf_dir / "sample" / "W1.pdf").exists())

            # FAILED is retried on the next run even without --force (base.needs_attempt).
            http_client.return_value.get_bytes.side_effect = None
            http_client.return_value.get_bytes.return_value = _make_pdf_bytes(["ok, nothing here"])
            CodeLinksStage(prepared, raw, config=config).run()
            rows = {row.id: row for row in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)

    @patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_code_links_falls_back_to_crawler_when_no_pdf_url(self, http_client):
        pdf_bytes = _make_pdf_bytes(["From the crawler: https://github.com/org/repo"])

        def get_bytes(url, retries=3):
            self.assertTrue(url.startswith("http://crawler.local/api/v1/"))
            if url.endswith("/health"):
                return b"ok"
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["url"], ["https://doi.org/10.1234/abc"])
            return pdf_bytes

        http_client.return_value.get_bytes.side_effect = get_bytes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Settings(data_dir=root / "data", pdf_crawler_url="http://crawler.local/api/v1")
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="t", doi="10.1234/abc"),
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="t", doi="10.1234/abc"),
            ])

            # Not configured at all: health is never probed, download never attempted.
            CodeLinksStage(prepared, raw, config=Settings(data_dir=root / "data")).run()
            http_client.return_value.get_bytes.assert_not_called()
            rows = {row.id: row for row in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)

            # Configured but unreachable: health probe fails closed, no FAILED status.
            config = Settings(data_dir=root / "data", pdf_crawler_url="http://crawler.local/api/v1")
            CodeLinksStage(prepared, raw, config=config, force=True).run()
            rows = {row.id: row for row in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)
            self.assertIsNone(rows["W1"].full_text)


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


if __name__ == "__main__":
    unittest.main()
