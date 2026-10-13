import unittest
from unittest.mock import Mock, patch

import fitz

from pauk.models import CodeLink, LinkOccurrence, RepoLink
from pauk.pipeline.stages.code_links import (
    _collect_occurrences,
    _extract_pdf,
    _link_reference_contexts,
    _occurrences_in_text,
    _pdf_page_occurrences,
    _preserve_pdf_occurrences,
)


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    try:
        return doc.tobytes()
    finally:
        doc.close()


def _make_pdf_with_duplicate_link_labels(uri: str, mixed_case: bool = False) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    first_line = "First here is unrelated. Another HERE is unrelated." if mixed_case else "First here is unrelated."
    page.insert_text((72, 72), first_line)
    page.insert_text((72, 100), "Second here points to the repository.")
    labels = page.search_for("here")
    page.insert_link({"kind": fitz.LINK_URI, "uri": uri, "from": labels[-1]})
    try:
        return doc.tobytes()
    finally:
        doc.close()


class ExtractPdfTest(unittest.TestCase):
    def test_pages_without_repository_links_skip_reference_scanning(self):
        with patch("pauk.pipeline.stages.code_links._REFERENCE_HEADING") as heading:
            _link_reference_contexts(["References\n1. Other work"], [{}])
            heading.search.assert_not_called()

    def collect_pages(self, pages):
        per_page = [_occurrences_in_text(text, number) for number, text in enumerate(pages, 1)]
        _link_reference_contexts(pages, per_page)
        return _collect_occurrences("", per_page)

    def test_reference_spelling_confirms_wrapped_url_after_source_relabeling(self):
        found = self.collect_pages([
            "We use [14].",
            "References\n14. Code github.com/keras-team/keras\n"
            "14. Code github.com/keras-\nteam/keras",
        ])
        self.assertEqual(list(found), ["https://github.com/keras-team/keras"])
        self.assertTrue(all(len(o.candidate_urls) == 1 for o in next(iter(found.values()))))

    def test_reference_number_can_be_on_a_separate_line(self):
        url = "https://github.com/org/repo"
        found = self.collect_pages(["We release our code [15].", f"References\n15.\nCode {url}"])
        self.assertEqual([(o.source, o.reference_label) for o in found[url]], [("citation", "15"), ("reference", "15")])

    def test_reference_entry_continues_on_the_next_page(self):
        url = "https://github.com/org/repo"
        found = self.collect_pages(["We release [9].", "References\n[9] Our code:", url + "\n[10] Other work"])
        self.assertEqual([(o.source, o.reference_label) for o in found[url]], [("citation", "9"), ("reference", "9")])

    def test_years_and_page_numbers_do_not_become_reference_labels(self):
        url = "https://github.com/org/repo"
        for text in (
            f"References\nAuthor.\n2016. A paper.\nAuthor.\n2017. Code {url}",
            f"References\n1.\nOther work. Pages 627-\n633. More information.\n2.\nCode {url}\n3.\nOther work.",
        ):
            with self.subTest(text=text):
                [occurrence] = self.collect_pages([text])[url]
                self.assertEqual(occurrence.source, "reference")
                self.assertIn(occurrence.reference_label, (None, "2"))

    def test_lettered_appendix_is_not_part_of_author_year_bibliography(self):
        url = "https://github.com/org/repo"
        for heading in ("A Comment Extraction", "A\nComment Extraction"):
            with self.subTest(heading=heading):
                found = self.collect_pages([f"References\nAuthor.\n2022. A paper.\n{heading}\nOur code: {url}"])
                [occurrence] = found[url]
                self.assertEqual(occurrence.source, "pdf_text")
                self.assertIsNone(occurrence.reference_label)

    def test_footnote_numbers_are_scoped_to_their_own_page(self):
        first = "https://github.com/org/first"
        second = "https://github.com/org/second"
        found = self.collect_pages([f"Our code [1].\n[1] {first}", f"Other code [1].\n[1] {second}"])
        self.assertEqual([o.page_number for o in found[first] if o.source == "citation"], [1])
        self.assertEqual([o.page_number for o in found[second] if o.source == "citation"], [2])

    def test_annotation_fallback_uses_overlapping_words_not_nearby_lines(self):
        url = "https://github.com/org/repo"
        text = "unrelated\nOur code is here\n"
        page = Mock()
        page.get_links.return_value = [{"kind": fitz.LINK_URI, "uri": url, "from": fitz.Rect(70, 89, 100, 102)}]
        page.get_text.side_effect = lambda kind, **kwargs: (
            [(0, 0, 20, 10, "unrelated"), (0, 90, 20, 100, "Our"),
             (21, 90, 40, 100, "code"), (41, 90, 50, 100, "is"), (71, 90, 95, 100, "here")]
            if kind == "words" else "d\nhere"
        )
        [occurrence] = _pdf_page_occurrences(page, text, 1)[url]
        self.assertEqual(occurrence.text_start, text.index("here"))
        self.assertIn("Our code is here", occurrence.context)
        self.assertFalse(occurrence.continuous)

    def test_annotation_word_fallback_failure_keeps_the_original_label(self):
        url = "https://github.com/org/repo"
        page = Mock()
        page.get_links.return_value = [{"kind": fitz.LINK_URI, "uri": url, "from": fitz.Rect(0, 0, 10, 10)}]

        def get_text(kind, **kwargs):
            if kind == "words":
                raise RuntimeError("words unavailable")
            return "partial label"

        page.get_text.side_effect = get_text
        [occurrence] = _pdf_page_occurrences(page, "Our source code", 1)[url]
        self.assertEqual(occurrence.context, "partial label")
        self.assertIsNone(occurrence.text_start)

    def test_case_variants_merge_without_losing_separate_occurrences(self):
        found = self.collect_pages(["github.com/org/Repo and github.com/ORG/repo"])
        self.assertEqual(len(found), 1)
        self.assertEqual(len(next(iter(found.values()))), 2)

    def test_other_pdf_cannot_confirm_a_wrapped_spelling(self):
        wrapped = _occurrences_in_text("github.com/org/my-\nrepo", 1)
        direct = _occurrences_in_text("github.com/org/my-repo", 1)
        for found, pdf_id in ((wrapped, "W1"), (direct, "W2")):
            for occurrences in found.values():
                for occurrence in occurrences:
                    occurrence.pdf_id = pdf_id
        found = _collect_occurrences("", [wrapped, direct])
        self.assertIn("https://github.com/org/myrepo", found)

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

    def test_hyperlink_offsets_match_case_insensitive_pdf_search(self):
        url = "https://github.com/org/repo"
        pages, page_occurrences = _extract_pdf(_make_pdf_with_duplicate_link_labels(url, mixed_case=True))

        annotation = next(
            occurrence for occurrence in page_occurrences[0][url]
            if occurrence.source == "pdf_annotation"
        )
        self.assertEqual(annotation.text_start, pages[0].rfind("here"))
        self.assertEqual(annotation.text_end, annotation.text_start + len("here"))
        self.assertIn("Second here points to the repository", annotation.context)

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
