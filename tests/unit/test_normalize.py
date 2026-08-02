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
            self.assertEqual({p.id for p in people}, {"A1", "A2"})
            self.assertEqual({p.id: p.is_itmo for p in people}, {"A1": True, "A2": False})
            self.assertEqual(sum(len(p.authored) for p in people), 2)

    def test_author_with_mixed_affiliations_stays_one_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W1", "title": "With ITMO affiliation",
                "authorships": [
                    {"author": {"id": "https://openalex.org/A1", "display_name": "Same Person"},
                     "institutions": [{"ror": "https://ror.org/04txgxn49"}]},
                ],
            }, {"work_id": "W1"})
            raw.append("openalex_works", {
                "id": "https://openalex.org/W2", "title": "Affiliation missed by OpenAlex",
                "authorships": [
                    {"author": {"id": "https://openalex.org/A1", "display_name": "Same Person"}},
                ],
            }, {"work_id": "W2"})
            prepared = PreparedStore(root / "prepared", "sample")
            result = OpenAlexNormalizer(raw, prepared).run()
            self.assertEqual(result, {"publications": 2, "persons": 1})
            person = next(prepared.read_models("persons", Person))
            self.assertEqual(person.id, "A1")
            self.assertTrue(person.is_itmo)
            self.assertEqual({a.publication_id for a in person.authored}, {"W1", "W2"})

    def test_legacy_prefixed_person_rows_merge_on_renormalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W3", "title": "New work", "authorships": [],
            }, {"work_id": "W3"})
            prepared = PreparedStore(root / "prepared", "sample")
            prepared.write_models("persons", [
                Person(id="itmo_A1", openalex_id="A1", is_itmo=True, name_en="Same Person",
                       orcid="0000-0001", authored=[{"publication_id": "W1", "position": 1}]),
                Person(id="external_A1", openalex_id="A1", is_itmo=False, name_en="Same Person",
                       email="p@example.org", authored=[{"publication_id": "W2", "position": 2}]),
            ])
            OpenAlexNormalizer(raw, prepared).run()
            people = list(prepared.read_models("persons", Person))
            self.assertEqual(len(people), 1)
            person = people[0]
            self.assertEqual(person.id, "A1")
            self.assertTrue(person.is_itmo)
            self.assertEqual(person.orcid, "0000-0001")
            self.assertEqual(person.email, "p@example.org")
            self.assertEqual({a.publication_id for a in person.authored}, {"W1", "W2"})

    def test_merged_author_id_keeps_routing_to_canonical_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W5", "title": "New work by the merged id",
                "authorships": [
                    {"author": {"id": "https://openalex.org/A9", "display_name": "Nikolay Nikitin"}},
                ],
            }, {"work_id": "W5"})
            prepared = PreparedStore(root / "prepared", "sample")
            prepared.write_models("persons", [
                Person(id="A1", openalex_id="A1", is_itmo=True, name_en="Nikolay O. Nikitin",
                       merged_ids=["A9"], authored=[{"publication_id": "W1", "position": 1}]),
            ])
            result = OpenAlexNormalizer(raw, prepared).run()
            self.assertEqual(result["persons"], 1)
            person = next(prepared.read_models("persons", Person))
            self.assertEqual(person.id, "A1")
            self.assertEqual(person.merged_ids, ["A9"])
            self.assertEqual({a.publication_id for a in person.authored}, {"W1", "W5"})

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
