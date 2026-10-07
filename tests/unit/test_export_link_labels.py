import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mongomock

from pauk.models import CodeLink, LinkOccurrence, Publication, RepoLink
from pauk.storage import PreparedStore
from scripts.export_link_labels import export_links, export_papers


class ExportLinkLabelsTest(unittest.TestCase):
    def setUp(self):
        self.store = PreparedStore(mongomock.MongoClient()["pauk_test"], "sample")

    def test_links_export_contains_every_context_seen_by_the_model(self):
        self.store.write_models("publications", [Publication(id="W1", title="Paper")])
        self.store.write_models("repo_links", [RepoLink(
            publication_id="W1",
            links=[CodeLink(
                url="https://github.com/org/repo",
                availability="failed",
                occurrences=[
                    LinkOccurrence(context="Abstract context", source="abstract"),
                    LinkOccurrence(
                        context="Body citation",
                        page_number=4,
                        source="citation",
                        reference_label="9",
                    ),
                ],
            )],
        )])

        [row] = export_links(self.store, limit=None, seed=42)

        self.assertIn("Abstract context", row["model_context"])
        self.assertIn("Body citation", row["model_context"])
        self.assertEqual(row["model_page"], "abstract, 4")
        self.assertEqual(row["full_context"], row["model_context"])
        self.assertEqual(row["resolved_on_github"], "failed")

    def test_papers_export_uses_the_flat_pdf_store_path(self):
        self.store.write_models("publications", [Publication(
            id="W1",
            title="Paper",
            full_text="text",
        )])
        with tempfile.TemporaryDirectory() as folder:
            pdf_dir = Path(folder)
            local_pdf = pdf_dir / "W1.pdf"
            local_pdf.write_bytes(b"pdf")
            with patch(
                "scripts.export_link_labels.settings",
                SimpleNamespace(pdf_dir=pdf_dir),
            ):
                [row] = export_papers(self.store, limit=None, seed=42, show_extracted=False)

        self.assertEqual(row["local_pdf"], str(local_pdf))


if __name__ == "__main__":
    unittest.main()
