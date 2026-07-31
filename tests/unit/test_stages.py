import tempfile
import unittest
from pathlib import Path

from pauk.models import Publication
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.code_links import CodeLinksStage
from pauk.pipeline.stages.base import PreparedSelection
from pauk.storage import PreparedStore, RawStore


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


if __name__ == "__main__":
    unittest.main()
