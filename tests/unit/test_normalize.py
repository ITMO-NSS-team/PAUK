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

    def test_re_normalization_routes_a_merged_work_to_its_publication(self):
        # Both records of one work are still in raw; the dedup stage folded
        # them, so re-normalizing must not resurrect the merged-away record.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            for work_id in ("W1", "W2"):
                raw.append("openalex_works", {
                    "id": f"https://openalex.org/{work_id}", "title": "One work",
                    "doi": "https://doi.org/10.1/x",
                    "authorships": [{"author": {"id": "https://openalex.org/A1",
                                                "display_name": "Author One"}}],
                }, {"work_id": work_id})
            prepared = PreparedStore(root / "prepared", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="One work", merged_ids=["W2"],
                            versions=[{"openalex_id": "W1"}, {"openalex_id": "W2"}]),
            ])
            result = OpenAlexNormalizer(raw, prepared).run()
            self.assertEqual(result["publications"], 1)
            publication = next(prepared.read_models("publications", Publication))
            self.assertEqual((publication.id, publication.merged_ids), ("W1", ["W2"]))
            self.assertEqual(len(publication.versions), 2)
            person = next(prepared.read_models("persons", Person))
            self.assertEqual([a.publication_id for a in person.authored], ["W1"])

    def test_authors_without_an_openalex_id_are_kept(self):
        # Fresh OpenAlex records carry the authors but no author entity yet;
        # dropping them would leave the publication with no authors at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W1", "title": "Fresh record",
                "authorships": [
                    {"author": {"id": None, "display_name": "Ianina D. Moor",
                                "orcid": "https://orcid.org/0000-0002-1624-2659"},
                     "institutions": [{"ror": "https://ror.org/04txgxn49"}]},
                    {"author": {"id": None, "display_name": "D. V. Karlovets"}},
                    {"author": {"id": None, "display_name": None}},
                ],
            }, {"work_id": "W1"})
            prepared = PreparedStore(root / "prepared", "sample")
            result = OpenAlexNormalizer(raw, prepared).run()
            # The nameless authorship is the only one that cannot be keyed.
            self.assertEqual(result["persons"], 2)
            people = {p.id: p for p in prepared.read_models("persons", Person)}
            by_orcid = people["orcid_0000-0002-1624-2659"]
            self.assertEqual(by_orcid.name_en, "Ianina D. Moor")
            self.assertEqual(by_orcid.orcid, "0000-0002-1624-2659")
            self.assertIsNone(by_orcid.openalex_id)
            self.assertTrue(by_orcid.is_itmo)
            by_name = next(p for p in people.values() if p.name_en == "D. V. Karlovets")
            self.assertTrue(by_name.id.startswith("name_"))
            self.assertIsNone(by_name.openalex_id)

    def test_the_same_unidentified_author_is_one_person_across_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            for work_id in ("W1", "W2"):
                raw.append("openalex_works", {
                    "id": f"https://openalex.org/{work_id}", "title": f"Work {work_id}",
                    "authorships": [{"author": {"id": None, "display_name": "D. V. Karlovets"}}],
                }, {"work_id": work_id})
            prepared = PreparedStore(root / "prepared", "sample")
            result = OpenAlexNormalizer(raw, prepared).run()
            self.assertEqual(result["persons"], 1)
            person = next(prepared.read_models("persons", Person))
            self.assertEqual({a.publication_id for a in person.authored}, {"W1", "W2"})

    def test_publisher_markup_is_stripped_from_titles_and_abstracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W1",
                "title": ('Optical probing in monolayer <mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML">'
                          " <mml:msub> <mml:mi>WSe</mml:mi> <mml:mn>2</mml:mn> </mml:msub> </mml:math> via"
                          " diffraction"),
                "abstract_inverted_index": {"Grown": [0], "on": [1], "CaF": [2], "<sub>2</sub>": [3],
                                            "/Si(111)": [4], "in": [5], "<i>vacuo</i>": [6]},
                "authorships": [],
            }, {"work_id": "W1"})
            prepared = PreparedStore(root / "prepared", "sample")
            OpenAlexNormalizer(raw, prepared).run()
            publication = next(prepared.read_models("publications", Publication))
            self.assertEqual(publication.title,
                             "Optical probing in monolayer WSe2 via diffraction")
            self.assertEqual(publication.abstract, "Grown on CaF2/Si(111) in vacuo")

    def test_a_formula_keeps_a_space_that_is_its_own_element(self):
        # Publishers spell "ab initio" out letter by letter, with the gap
        # carried by <mml:mo> </mml:mo> — that space is content, not layout.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = RawStore(root / "raw", "sample")
            raw.append("openalex_works", {
                "id": "https://openalex.org/W1", "authorships": [],
                "title": ('Comparison of the <mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML">'
                          " <mml:mrow> <mml:mi>a</mml:mi> <mml:mi>b</mml:mi> </mml:mrow>"
                          " <mml:mo> </mml:mo> <mml:mrow> <mml:mi>i</mml:mi> <mml:mi>n</mml:mi>"
                          " <mml:mi>i</mml:mi> <mml:mi>t</mml:mi> <mml:mi>i</mml:mi> <mml:mi>o</mml:mi>"
                          " </mml:mrow> </mml:math> QED approaches"),
            }, {"work_id": "W1"})
            prepared = PreparedStore(root / "prepared", "sample")
            OpenAlexNormalizer(raw, prepared).run()
            publication = next(prepared.read_models("publications", Publication))
            self.assertEqual(publication.title, "Comparison of the ab initio QED approaches")

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
