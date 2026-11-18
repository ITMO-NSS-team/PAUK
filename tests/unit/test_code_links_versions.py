import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.models import (
    CodeLink,
    LinkOccurrence,
    Publication,
    PublicationSourceRecord,
    RepoLink,
)
from pauk.pipeline.stages.code_links import CodeLinksStage
from pauk.pipeline.stages.dedup import _merge_code_link
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from tests.unit.test_code_links_pdf import _make_pdf_bytes


class CodeLinksVersionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Settings(data_dir=Path(temporary.name), pdf_crawler_url="")
        self.config.pdf_dir.mkdir()
        self.db = mongomock.MongoClient().pauk_test
        self.prepared = PreparedStore(self.db, "sample")

    def run_stage(self):
        with patch("pauk.pipeline.stages.code_links.HttpClient") as http:
            CodeLinksStage(self.prepared, RawStore(self.db, "sample"), self.config, force=True).run()
            http.return_value.get_bytes.assert_not_called()
        return next(self.prepared.read_models("repo_links", RepoLink))

    def test_cached_merged_version_is_read_without_pdf_urls_and_keeps_its_page_numbers(self):
        self.prepared.write_models("publications", [Publication(
            id="W1", title="paper", merged_ids=["W2"], versions=[{"openalex_id": "W2"}],
        )])
        (self.config.pdf_dir / "W1.pdf").write_bytes(_make_pdf_bytes("Short version without code."))
        (self.config.pdf_dir / "W2.pdf").write_bytes(_make_pdf_bytes("Our code: https://github.com/org/repo"))
        [link] = self.run_stage().links
        self.assertEqual([(o.pdf_id, o.page_number) for o in link.occurrences], [("W2", 1)])
        publication = next(self.prepared.read_models("publications", Publication))
        self.assertIn("Short version", publication.full_text)
        self.assertIn("Our code", publication.full_text)
        self.assertEqual(publication.processing["code_links"].status, "completed")

    def test_only_cached_version_can_supply_the_publication_pdf(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="paper", versions=[{"openalex_id": "W2"}])])
        (self.config.pdf_dir / "W2.pdf").write_bytes(_make_pdf_bytes("https://github.com/org/repo"))
        [link] = self.run_stage().links
        self.assertEqual(link.occurrences[0].pdf_id, "W2")
        self.assertFalse((self.config.pdf_dir / "W1.pdf").exists())

    def test_cached_version_avoids_downloading_a_missing_main_pdf(self):
        self.prepared.write_models("publications", [Publication(
            id="W1", title="paper", pdf_urls=["https://example.org/main.pdf"], merged_ids=["W2"],
        )])
        (self.config.pdf_dir / "W2.pdf").write_bytes(_make_pdf_bytes("https://github.com/org/repo"))
        [link] = self.run_stage().links
        self.assertEqual(link.occurrences[0].pdf_id, "W2")
        publication = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(publication.processing["code_links"].status, "completed")

    def test_broken_version_cache_does_not_block_recovery_from_primary_sources(self):
        self.prepared.write_models("publications", [Publication(
            id="W1", title="paper", pdf_urls=["https://example.org/main.pdf"], merged_ids=["W2"],
        )])
        (self.config.pdf_dir / "W2.pdf").write_bytes(b"broken pdf")
        with patch("pauk.pipeline.stages.code_links.HttpClient") as http:
            http.return_value.get_bytes.return_value = _make_pdf_bytes("https://github.com/org/recovered")
            CodeLinksStage(self.prepared, RawStore(self.db, "sample"), self.config).run()
            http.return_value.get_bytes.assert_called_once_with("https://example.org/main.pdf")
        [link] = next(self.prepared.read_models("repo_links", RepoLink)).links
        self.assertEqual(link.url, "https://github.com/org/recovered")
        self.assertEqual(link.occurrences[0].pdf_id, "W1")
        publication = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(publication.processing["code_links"].status, "failed")

    def test_unavailable_article_record_falls_back_to_preprint_record_key(self):
        self.prepared.write_models("publications", [Publication(
            id="pub_1",
            title="paper",
            schema_version=2,
            source_records=[
                PublicationSourceRecord(
                    key="catalog:article",
                    source="catalog",
                    source_id="article",
                    title="paper",
                    type="article",
                    doi="10.1/article",
                ),
                PublicationSourceRecord(
                    key="archive:preprint",
                    source="archive",
                    source_id="preprint",
                    title="paper",
                    type="preprint",
                    pdf_urls=["https://example.org/preprint.pdf"],
                ),
            ],
        )])
        with patch("pauk.pipeline.stages.code_links.HttpClient") as http:
            http.return_value.get_bytes.return_value = _make_pdf_bytes(
                "https://github.com/org/preprint-code"
            )
            CodeLinksStage(
                self.prepared, RawStore(self.db, "sample"), self.config,
            ).run()
            http.return_value.get_bytes.assert_called_once_with(
                "https://example.org/preprint.pdf"
            )
        [link] = next(self.prepared.read_models("repo_links", RepoLink)).links
        self.assertEqual(link.occurrences[0].pdf_id, "archive:preprint")

    def test_partial_failure_preserves_failed_version_not_replaced_main_evidence(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.prepared.write_models("publications", [Publication(
                    id="W1", title="paper", merged_ids=["W2"], full_text="Previously complete text",
                )])
                self.prepared.write_models("repo_links", [RepoLink(publication_id="W1", links=[
                    CodeLink(url="https://github.com/org/old-main", occurrences=[LinkOccurrence(pdf_id="W1", page_number=1)]),
                    CodeLink(url="https://github.com/org/version", occurrences=[LinkOccurrence(pdf_id="W2", page_number=1)]),
                ])])
                (self.config.pdf_dir / "W1.pdf").write_bytes(_make_pdf_bytes("https://github.com/org/current"))
                alias = self.config.pdf_dir / "W2.pdf"
                if not missing:
                    alias.write_bytes(b"broken pdf")
                elif alias.exists():
                    alias.unlink()
                row = self.run_stage()
                self.assertEqual({link.url for link in row.links}, {"https://github.com/org/current", "https://github.com/org/version"})
                publication = next(self.prepared.read_models("publications", Publication))
                self.assertEqual(publication.processing["code_links"].status, "failed")
                self.assertEqual(publication.full_text, "Previously complete text")

    def test_identical_context_in_two_versions_keeps_both_provenances(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="paper", merged_ids=["W2"])])
        data = _make_pdf_bytes("https://github.com/org/repo")
        for pdf_id in ("W1", "W2"):
            (self.config.pdf_dir / f"{pdf_id}.pdf").write_bytes(data)
        [link] = self.run_stage().links
        self.assertEqual([o.pdf_id for o in link.occurrences], ["W1", "W2"])
        extra = CodeLink(url=link.url, occurrences=[LinkOccurrence(context="same", page_number=1, pdf_id="W3")])
        _merge_code_link(link, extra)
        self.assertEqual([o.pdf_id for o in link.occurrences], ["W1", "W2", "W3"])

    def test_dedup_does_not_collapse_identical_occurrences_from_different_pdfs(self):
        first = CodeLink(url="https://github.com/org/repo", occurrences=[LinkOccurrence(context="same", page_number=1, pdf_id="W1")])
        second = CodeLink(url=first.url, occurrences=[LinkOccurrence(context="same", page_number=1, pdf_id="W2")])
        _merge_code_link(first, second)
        self.assertEqual([o.pdf_id for o in first.occurrences], ["W1", "W2"])

    def test_archived_repository_case_variant_does_not_create_a_duplicate_link(self):
        self.prepared.write_models("publications", [Publication(
            id="W1", title="Org/Repo: Release", type="software", abstract="https://github.com/org/repo",
        )])
        [link] = self.run_stage().links
        self.assertTrue(link.is_relevant)
        self.assertEqual(len(link.occurrences), 2)
