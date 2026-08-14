import tempfile
import unittest
from pathlib import Path

import mongomock

from pauk.storage import PdfStore


class PdfStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf_dir = Path(tmp.name) / "pdf"

    def test_exists_is_false_before_save(self):
        store = PdfStore(self.db, self.pdf_dir)
        self.assertFalse(store.exists("W1"))

    def test_save_then_read_round_trips_the_bytes(self):
        store = PdfStore(self.db, self.pdf_dir)
        store.save("W1", b"%PDF-1.4 fake bytes")
        self.assertTrue(store.exists("W1"))
        self.assertEqual(store.read("W1"), b"%PDF-1.4 fake bytes")

    def test_save_creates_pdf_dir_if_missing(self):
        # pdf_dir doesn't exist yet at all (not even its parent) - nothing
        # pre-creates it, save() must.
        self.assertFalse(self.pdf_dir.exists())
        PdfStore(self.db, self.pdf_dir).save("W1", b"data")
        self.assertTrue((self.pdf_dir / "W1.pdf").exists())

    def test_save_records_a_pointer_with_fetched_at(self):
        PdfStore(self.db, self.pdf_dir).save("W1", b"data")
        pointer = self.db.pdfs.find_one({"_id": "W1"})
        assert pointer is not None
        self.assertIn("fetched_at", pointer)

    def test_save_overwrites_the_file_on_a_later_call(self):
        store = PdfStore(self.db, self.pdf_dir)
        store.save("W1", b"old bytes")
        store.save("W1", b"new bytes")
        self.assertEqual(store.read("W1"), b"new bytes")

    def test_file_handle_is_released_after_read(self):
        # PR #45's regression (WinError 32: a handle held open across a delete)
        # is only possible if something holds the file open past its call -
        # read() must open, read, and close in one step so the caller is free
        # to replace or remove the file immediately after.
        store = PdfStore(self.db, self.pdf_dir)
        store.save("W1", b"data")
        store.read("W1")
        path = self.pdf_dir / "W1.pdf"
        path.unlink()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
