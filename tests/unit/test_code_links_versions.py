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
from pauk.storage import PdfStore, PreparedStore, RawStore
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
            http.return_value.get_bytes.assert_called_once()
            self.assertEqual(http.return_value.get_bytes.call_args.args, ("https://example.org/main.pdf",))
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
            http.return_value.get_bytes.assert_called_once()
            self.assertEqual(http.return_value.get_bytes.call_args.args, ("https://example.org/preprint.pdf",))
        [link] = next(self.prepared.read_models("repo_links", RepoLink)).links
        self.assertEqual(link.occurrences[0].pdf_id, "archive:preprint")
        store = PdfStore(self.db, self.config.pdf_dir)
        self.assertTrue(store.exists("archive:preprint"))
        self.assertFalse(store.exists("pub_1"))

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

    def test_corrupt_preprint_is_replaced_even_when_another_version_is_readable(self):
        preprint = "archive:preprint"
        url = "https://example.org/preprint.pdf"
        store = PdfStore(self.db, self.config.pdf_dir)
        recovered = _make_pdf_bytes("https://github.com/org/recovered")
        for cached_article in (False, True):
            with self.subTest(cached_article=cached_article):
                self.prepared.write_models("publications", [Publication(
                    id="pub_1", title="paper", schema_version=2, source_records=[
                        {"source": "catalog", "source_id": "article", "doi": "10.1/article"},
                        {"source": "archive", "source_id": "preprint", "pdf_urls": [url]},
                    ],
                )])
                store.save(preprint, b"broken")
                if cached_article:
                    store.save("catalog:article", _make_pdf_bytes("Article without code"))
                with patch("pauk.pipeline.stages.code_links.HttpClient") as http:
                    http.return_value.get_bytes.return_value = recovered
                    CodeLinksStage(self.prepared, RawStore(self.db, "sample"), self.config).run()
                    self.assertEqual([call.args for call in http.return_value.get_bytes.call_args_list], [(url,)])
                self.assertEqual(store.read(preprint), recovered)
                publication = next(self.prepared.read_models("publications", Publication))
                self.assertEqual(publication.processing["code_links"].status, "completed")
                [link] = self.run_stage().links
                self.assertEqual([occ.pdf_id for occ in link.occurrences], [preprint])

    def test_redownloads_a_missing_version_that_previously_supplied_links(self):
        preprint = "archive:preprint"
        url = "https://example.org/preprint.pdf"
        store = PdfStore(self.db, self.config.pdf_dir)
        pub = Publication(id="pub_1", title="paper", schema_version=2, source_records=[
            {"source": "catalog", "source_id": "article", "doi": "10.1/article"},
            {"source": "archive", "source_id": "preprint", "pdf_urls": [url]},
        ])
        self.prepared.write_models("publications", [pub])
        store.save("catalog:article", _make_pdf_bytes("Article without code"))
        self.prepared.write_models("repo_links", [RepoLink(publication_id=pub.id, links=[
            CodeLink(url="https://github.com/org/stale",
                     occurrences=[LinkOccurrence(page_number=1, pdf_id=preprint)]),
        ])])
        with patch("pauk.pipeline.stages.code_links.HttpClient") as http:
            http.return_value.get_bytes.return_value = _make_pdf_bytes("https://github.com/org/recovered")
            CodeLinksStage(self.prepared, RawStore(self.db, "sample"), self.config).run()
            self.assertEqual([call.args for call in http.return_value.get_bytes.call_args_list], [(url,)])
        [link] = next(self.prepared.read_models("repo_links", RepoLink)).links
        self.assertEqual(link.url, "https://github.com/org/recovered")
        self.assertEqual(link.occurrences[0].pdf_id, preprint)
        publication = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(publication.processing["code_links"].status, "completed")

    def test_legacy_evidence_is_replaced_only_when_the_same_file_was_read(self):
        store = PdfStore(self.db, self.config.pdf_dir)
        store.save("W1", _make_pdf_bytes("https://github.com/org/current"))
        for separate_files, broken_legacy in ((False, False), (True, False), (True, True)):
            with self.subTest(separate_files=separate_files, broken_legacy=broken_legacy):
                self.prepared.write_models("publications", [Publication(
                    id="pub_1", title="paper", schema_version=2,
                    source_records=[{"source": "openalex", "source_id": "W1", "doi": "10.1/article"}],
                )])
                self.prepared.write_models("repo_links", [RepoLink(publication_id="pub_1", links=[CodeLink(
                    url="https://github.com/org/old", occurrences=[LinkOccurrence(pdf_id="W1", page_number=1)],
                )])])
                if separate_files:
                    store.save("openalex:W1", _make_pdf_bytes("https://github.com/org/current"))
                if broken_legacy:
                    store.save("W1", b"broken")
                links = self.run_stage().links
                evidence = {(link.url, occ.pdf_id) for link in links for occ in link.occurrences}
                expected = {("https://github.com/org/current", "openalex:W1")}
                if separate_files:
                    expected.add(("https://github.com/org/old" if broken_legacy else "https://github.com/org/current", "W1"))
                self.assertEqual(evidence, expected)
                publication = next(self.prepared.read_models("publications", Publication))
                self.assertEqual(publication.processing["code_links"].status, "failed" if broken_legacy else "completed")

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
