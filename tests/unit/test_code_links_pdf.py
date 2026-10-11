import unittest

import fitz

from pauk.models import CodeLink, LinkOccurrence, RepoLink
from pauk.pipeline.stages.code_links import (
    _collect_occurrences,
    _extract_pdf,
    _link_reference_contexts,
    _occurrences_in_text,
    _preserve_pdf_occurrences,
)


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    try:
        return doc.tobytes()
    finally:
        doc.close()


def _make_pdf_with_duplicate_link_labels(uri: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "First here is unrelated.")
    page.insert_text((72, 100), "Second here points to the repository.")
    labels = page.search_for("here")
    page.insert_link({"kind": fitz.LINK_URI, "uri": uri, "from": labels[1]})
    try:
        return doc.tobytes()
    finally:
        doc.close()


class ExtractPdfTest(unittest.TestCase):
    def test_extract_pdf_returns_per_page_text(self):
        pdf_bytes = _make_pdf_bytes("See https://github.com/octocat/Hello-World for the code.")
        pages, page_occurrences = _extract_pdf(pdf_bytes)
        self.assertEqual(len(pages), 1)
        self.assertIn("github.com/octocat/Hello-World", pages[0])

    def test_hyperlink_uses_the_visible_label_nearest_its_rectangle(self):
        url = "https://github.com/org/repo"

        pages, page_occurrences = _extract_pdf(_make_pdf_with_duplicate_link_labels(url))

        annotation = next(
            occurrence for occurrence in page_occurrences[0][url]
            if occurrence.source == "pdf_annotation"
        )
        self.assertEqual(annotation.text_start, pages[0].rfind("here"))
        self.assertEqual(annotation.text_end, annotation.text_start + len("here"))

    def test_numbered_reference_adds_the_body_citation_context(self):
        pages = [
            "Our implementation and trained models are available in [8-10].",
            "6. References:\n[8] Other work.\n"
            "[09]. Project source https://github.com/org/repo\n"
            "[10] Another work.",
        ]
        per_page = [
            _occurrences_in_text(text, page_number)
            for page_number, text in enumerate(pages, 1)
        ]

        _link_reference_contexts(pages, per_page)
        occurrences = _collect_occurrences("", per_page)["https://github.com/org/repo"]

        self.assertEqual(
            [(occurrence.source, occurrence.page_number, occurrence.reference_label)
             for occurrence in occurrences],
            [("citation", 1, "9"), ("reference", 2, "9")],
        )
        self.assertIn("Our implementation", occurrences[0].context)

    def test_appendix_after_references_is_body_text_again(self):
        repository_url = "https://github.com/org/repo"
        appendix_url = "https://github.com/org/appendix"
        pages = [
            "We use the released implementation [9].",
            f"References\n[9] Project source {repository_url}",
            "Appendix A: Extra experiments\n"
            f"1. Experimental code {appendix_url}\n"
            "The repository from [9] is used for this experiment.",
        ]
        per_page = [
            _occurrences_in_text(text, page_number)
            for page_number, text in enumerate(pages, 1)
        ]

        _link_reference_contexts(pages, per_page)

        repository_occurrences = _collect_occurrences("", per_page)[repository_url]
        citation_pages = [
            occurrence.page_number for occurrence in repository_occurrences
            if occurrence.source == "citation"
        ]
        self.assertEqual(citation_pages, [1, 3])
        [appendix_occurrence] = per_page[2][appendix_url]
        self.assertEqual(appendix_occurrence.source, "pdf_text")
        self.assertIsNone(appendix_occurrence.reference_label)

    def test_citation_does_not_resolve_an_ambiguous_wrapped_url(self):
        pages = [
            "We release the implementation in [9].",
            "References\n[9] Code https://github.com/org/my-\nrepo",
        ]
        per_page = [
            _occurrences_in_text(text, page_number)
            for page_number, text in enumerate(pages, 1)
        ]

        _link_reference_contexts(pages, per_page)
        links = [
            CodeLink(url=url, occurrences=occurrences)
            for url, occurrences in _collect_occurrences("", per_page).items()
        ]

        self.assertEqual(len(links), 2)
        self.assertTrue(all(link.url_ambiguous for link in links))

    def test_distinct_urls_in_one_reference_are_not_url_alternatives(self):
        pages = [
            "We publish code and data in [9].",
            "References\n[9] Code https://github.com/org/code and "
            "data https://github.com/org/data",
        ]
        per_page = [
            _occurrences_in_text(text, page_number)
            for page_number, text in enumerate(pages, 1)
        ]

        _link_reference_contexts(pages, per_page)
        links = [
            CodeLink(url=url, occurrences=occurrences)
            for url, occurrences in _collect_occurrences("", per_page).items()
        ]

        self.assertEqual(len(links), 2)
        self.assertTrue(all(not link.url_ambiguous for link in links))
        for link in links:
            citation = next(
                occurrence for occurrence in link.occurrences
                if occurrence.source == "citation"
            )
            self.assertEqual(citation.candidate_urls, [link.url])

    def test_numbered_footnote_without_heading_adds_its_body_context(self):
        pages = [
            "We publish the complete implementation in [01].\n"
            "[01] https://github.com/org/repo",
        ]
        per_page = [_occurrences_in_text(pages[0], 1)]

        _link_reference_contexts(pages, per_page)
        occurrences = _collect_occurrences("", per_page)["https://github.com/org/repo"]

        self.assertEqual(
            [(occurrence.source, occurrence.reference_label) for occurrence in occurrences],
            [("reference", "1"), ("citation", "1")],
        )
        self.assertTrue(any("publish the complete implementation" in occurrence.context
                            for occurrence in occurrences))

    def test_failed_retry_preserves_distinct_evidence_with_the_same_context(self):
        url = "https://github.com/org/repo"
        previous = RepoLink(publication_id="W1", links=[CodeLink(
            url=url,
            occurrences=[
                LinkOccurrence(context="same", page_number=1, source="pdf_text"),
                LinkOccurrence(context="same", page_number=1, source="pdf_annotation"),
            ],
        )])

        preserved = _preserve_pdf_occurrences({}, previous)

        self.assertEqual(
            [occurrence.source for occurrence in preserved[url]],
            ["pdf_text", "pdf_annotation"],
        )


if __name__ == "__main__":
    unittest.main()
