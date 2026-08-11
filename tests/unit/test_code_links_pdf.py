import tempfile
import unittest
from pathlib import Path

import fitz

from pauk.pipeline.stages.code_links import _extract_pdf


def _make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


class ExtractPdfTest(unittest.TestCase):
    def test_extract_pdf_returns_per_page_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            _make_pdf(path, "See https://github.com/octocat/Hello-World for the code.")
            pages, page_occurrences = _extract_pdf(path)
            self.assertEqual(len(pages), 1)
            self.assertIn("github.com/octocat/Hello-World", pages[0])

    def test_extract_pdf_releases_the_file_handle(self):
        # PR #45: the pre-pauk fetch_papers.py read a downloaded file and unlinked
        # it while the handle was still open inside the `with` block — WinError 32
        # on Windows. The pauk extractor opens the PDF inside `with fitz.open(...)`,
        # so the handle is released before the caller touches the file. Deleting it
        # right after extraction must succeed (a leaked handle raises PermissionError
        # / WinError 32 on Windows; POSIX allows it but the intent is pinned here).
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            _make_pdf(path, "text with a link https://github.com/a/b")
            _extract_pdf(path)
            path.unlink()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
