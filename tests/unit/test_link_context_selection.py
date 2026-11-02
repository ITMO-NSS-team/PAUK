import unittest

from pauk.models import LinkOccurrence
from pauk.pipeline.stages.link_relevance import _format_contexts, _model_occurrences


class LinkContextSelectionTest(unittest.TestCase):
    def test_overlapping_annotations_do_not_crowd_out_later_pages(self):
        occurrences = [LinkOccurrence(
            context=f"Repeated fragment {index}", page_number=12, pdf_id="W1", source="pdf_annotation",
            text_start=1000 + index * 10, text_end=1030 + index * 10,
        ) for index in range(14)]
        occurrences.append(LinkOccurrence(context="Later evidence", page_number=28, pdf_id="W1", source="pdf_text"))
        selected = _model_occurrences(occurrences)
        self.assertEqual(len(selected), 2)
        self.assertIn(occurrences[-1], selected)
        self.assertEqual(len(occurrences), 15)

    def test_different_pdf_versions_are_not_deduplicated(self):
        occurrences = [LinkOccurrence(
            context="same", page_number=1, pdf_id=pdf_id, source="pdf_text", text_start=0, text_end=10,
        ) for pdf_id in ("W1", "W2")]
        formatted = _format_contexts(occurrences)
        self.assertEqual(len(_model_occurrences(occurrences)), 2)
        self.assertIn("PDF W1", formatted)
        self.assertIn("PDF W2", formatted)

    def test_distinct_sources_and_reference_labels_survive_overlap_selection(self):
        occurrences = [LinkOccurrence(
            context="same", page_number=1, text_start=0, text_end=10, source=source, reference_label=label,
        ) for source, label in (("pdf_text", None), ("pdf_annotation", None), ("reference", "1"), ("reference", "2"))]
        self.assertEqual(len(_model_occurrences(occurrences)), 4)

    def test_limit_prioritizes_new_pages_before_more_contexts_from_one_page(self):
        occurrences = [LinkOccurrence(context=f"first {index}", page_number=1, source="pdf_text") for index in range(20)]
        occurrences.append(LinkOccurrence(context="later", page_number=2, source="pdf_text"))
        selected = _model_occurrences(occurrences, 3)
        self.assertEqual(len(selected), 3)
        self.assertIn(occurrences[-1], selected)

    def test_unnumbered_reference_has_no_none_label_in_prompt(self):
        formatted = _format_contexts([LinkOccurrence(context="Reference", source="reference", page_number=5)])
        self.assertIn("reference list, page 5", formatted)
        self.assertNotIn("[None]", formatted)
