import unittest

import fitz

from pauk.pipeline.stages.code_links import _extract_pdf


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
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


if __name__ == "__main__":
    unittest.main()
