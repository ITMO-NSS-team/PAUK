import tempfile
import unittest
from pathlib import Path

from pauk.models import Person, Publication, RepoLink, Repository
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.storage import PreparedStore, RawStore


class NormalizeTest(unittest.TestCase):
    def test_openalex_work_creates_publication_and_all_authors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W1", "title": "Paper",
                "publication_date": "2025-01-02",
                "authorships": [
                    {"author": {"id": "https://openalex.org/A1", "display_name": "ITMO Author"},
                     "institutions": [{"ror": "https://ror.org/04txgxn49"}],
                     "raw_affiliation_strings": ["ITMO University"]},
                    {"author": {"id": "https://openalex.org/A2", "display_name": "External Author"}},
                ],
            }, {"work_id": "W1"})
            prepared = PreparedStore(root / "prepared", "sample")
            result = OpenAlexNormalizer(raw, prepared).run()
            self.assertEqual(result, {"publications": 1, "persons": 2})
            publication = next(prepared.read_models("publications", Publication))
            people = list(prepared.read_models("persons", Person))
            self.assertEqual(publication.id, "W1")
            self.assertEqual({p.id for p in people}, {"itmo_A1", "external_A2"})
            self.assertEqual(sum(len(p.authored) for p in people), 2)

    def test_re_normalization_preserves_enrichment_files_and_publication_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W1", "title": "Paper", "authorships": [],
            }, {"work_id": "W1"})
            prepared = PreparedStore(root / "prepared", "sample")
            prepared.write_models("publications", [Publication(id="W1", title="old", has_code=True)])
            prepared.write_models("repositories", [
                Repository(id="github_org_repo", name="repo", url="https://github.com/org/repo")
            ])
            prepared.write_models("repo_links", [
                RepoLink(publication_id="W1", links=[{"url": "https://github.com/org/repo"}])
            ])
            OpenAlexNormalizer(raw, prepared).run()
            publication = next(prepared.read_models("publications", Publication))
            self.assertEqual((publication.title, publication.has_code), ("Paper", True))
            self.assertEqual(len(list(prepared.read_models("repositories", Repository))), 1)
            self.assertEqual(len(list(prepared.read_models("repo_links", RepoLink))), 1)


if __name__ == "__main__":
    unittest.main()
