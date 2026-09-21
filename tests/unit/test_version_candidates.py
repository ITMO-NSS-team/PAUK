import unittest

from pauk.graph.version_candidates import (
    MERGE_BUCKETS,
    _candidate_pairs,
    classify_pair,
    find_version_candidates,
)


def pub(pid, title, *, doi=None, journal=None, type="article",  # noqa: A002 (matches OpenAlex's own field name)
        year=None, pdate=None, abstract=None, authors=()):
    return {
        "id": pid, "title": title, "doi": doi, "journal": journal, "type": type,
        "year": year, "publication_date": pdate, "abstract": abstract, "pdf_url": None,
        "openalex_url": None,
        "authors": [{"person_id": a, "name": a, "position": i} for i, a in enumerate(authors, 1)],
    }


class ClassifyPairTest(unittest.TestCase):
    def test_preprint_and_published_version_are_linked(self):
        bucket, signals = classify_pair(
            pub("W1", "Frequency Estimation of Multi-Sinusoidal Signals in Finite-Time",
                doi="10.48550/arxiv.2009.06400", journal="arXiv (Cornell University)",
                type="preprint", pdate="2020-09-14", authors=["A1", "A2", "A3"]),
            pub("W2", "Finite Time Frequency Estimation for Multi-Sinusoidal Signals",
                doi="10.1016/j.ejcon.2021.01.004", journal="European Journal of Control",
                type="article", pdate="2021-02-10", authors=["A1", "A2", "A3"]),
        )
        self.assertEqual(bucket, "PREPRINT_PUBLISHED")
        self.assertTrue(signals["preprint_split"])
        self.assertIn(bucket, MERGE_BUCKETS)

    def test_russian_original_and_english_translation_are_linked(self):
        bucket, _signals = classify_pair(
            pub("W1", "Impact of transverse optical confinment on performance of VCSELs",
                doi="10.21883/tpl.2022.14.55117", journal="Письма в журнал технической физики",
                pdate="2022-01-01", authors=["A1", "A2", "A3", "A4"]),
            pub("W2", "Impact of Transverse Optical Confinement on Performance of VCSELs",
                doi="10.1134/s1063785023900674", journal="Technical Physics Letters",
                pdate="2023-12-01", authors=["A1", "A2", "A3", "A4"]),
        )
        self.assertEqual(bucket, "RU_EN_TRANSLATION")

    def test_reworded_title_with_author_overlap_is_strong_fuzzy(self):
        bucket, signals = classify_pair(
            pub("W1", "Improvement of methods and means of thermal imagers verification and calibration",
                doi="10.1/a", pdate="2020-01-01", authors=["A1", "A2", "A3"]),
            pub("W2", "Improvement of Methods and Means for the Verification and Calibration of Thermal Imagers",
                doi="10.1/b", pdate="2020-09-01", authors=["A1", "A2", "A3"]),
        )
        self.assertEqual(bucket, "STRONG_FUZZY")
        self.assertGreaterEqual(signals["title_sim"], 0.90)

    def test_weakly_reworded_title_falls_to_medium_fuzzy(self):
        bucket, signals = classify_pair(
            pub("W1", "Method of the Joint Clustering in Network and Correlation Spaces",
                doi="10.1/a", pdate="2020-06-24", authors=["A1", "A2", "A3"]),
            pub("W2", "Method for Joint Clustering in Graph and Correlation Spaces",
                doi="10.1/b", pdate="2021-12-01", authors=["A1", "A2", "A3"]),
        )
        self.assertEqual(bucket, "MEDIUM_FUZZY")
        self.assertLess(signals["title_sim"], 0.90)

    def test_unrelated_titles_are_dropped(self):
        result = classify_pair(
            pub("W1", "Optical properties of icosahedral quasicrystals",
                pdate="2020-01-01", authors=["A1"]),
            pub("W2", "A survey of deep learning methods for protein structure prediction",
                pdate="2020-01-01", authors=["A1"]),
        )
        self.assertIsNone(result)

    def test_exact_title_after_folding_but_different_doi_is_missed_by_norm(self):
        bucket, signals = classify_pair(
            pub("W1", "Tapping into non-English-language science for the conservation of global biodiversity",
                doi="10.1371/journal.pbio.3001296"),
            pub("W2", "Tapping into non-English-language science for the conservation of global biodiversity.",
                doi="10.17863/cam.77861"),
        )
        self.assertEqual(bucket, "MISSED_BY_NORM")
        self.assertEqual(signals["title_sim"], 1.0)

    def test_doi_version_suffix_is_a_sibling(self):
        # A DOI-versioned sub-part legitimately carries its own sub-title
        # ("...84874" is the whole paper, "...84874.2" one of its revisions
        # with an editor's note appended) - the DOI relation, not the
        # title, is what proves these are the same work.
        bucket, _signals = classify_pair(
            pub("W1", "Expanding the stdpopsim species catalog", doi="10.7554/elife.84874",
                authors=["A1", "A2"]),
            pub("W2", "Expanding the stdpopsim species catalog: revision 2", doi="10.7554/elife.84874.2",
                authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "DOI_SIBLING")

    def test_correction_is_not_a_version(self):
        bucket, signals = classify_pair(
            pub("W1", "Animal model of assessing cerebrovascular functional reserve"),
            pub("W2", "Author Correction: Animal model of assessing cerebrovascular functional reserve"),
        )
        self.assertEqual(bucket, "ERRATUM")
        self.assertNotIn(bucket, MERGE_BUCKETS)
        self.assertEqual(signals["original"], "W1")

    def test_series_parts_are_not_a_version(self):
        bucket, _signals = classify_pair(
            pub("W1", 'Cliques and Constructors in "Hats" Game. I', authors=["A1", "A2"]),
            pub("W2", 'Cliques and Constructors in "Hats" Game. II', authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "SERIES_NOT_VERSION")
        self.assertNotIn(bucket, MERGE_BUCKETS)

    def test_book_volumes_are_not_a_version(self):
        bucket, _signals = classify_pair(
            pub("W1", "Symmetry in Quantum and Computational Chemistry"),
            pub("W2", "Symmetry in Quantum and Computational Chemistry: Volume 2"),
        )
        self.assertEqual(bucket, "SERIES_NOT_VERSION")

    def test_supplementary_file_tracking_a_paper_is_reported_not_merged(self):
        bucket, _signals = classify_pair(
            pub("W1", "Impact of immunosuppression on the incidence of ventilator-associated events"),
            pub("W2", "Additional file 1 of Impact of immunosuppression on the incidence "
                      "of ventilator-associated events"),
        )
        self.assertEqual(bucket, "SUPPLEMENT_OR_REVIEW")
        self.assertNotIn(bucket, MERGE_BUCKETS)

    def test_unrelated_deposit_sharing_only_an_author_is_dropped(self):
        result = classify_pair(
            pub("W1", "GADMA: Genetic algorithm for inferring demographic history",
                authors=["A1"]),
            pub("W2", "Data from: Whole-genome analysis of giraffe supports four distinct species",
                authors=["A1"]),
        )
        self.assertIsNone(result)

    def test_software_releases_are_excluded_from_fuzzy_matching(self):
        result = classify_pair(
            pub("W1", "asl/BandageNG: Release v2026.4.1", type="software", authors=["A1"]),
            pub("W2", "asl/BandageNG: Release v2026.6.1", type="software", authors=["A1"]),
        )
        self.assertIsNone(result)

    def test_generic_front_matter_title_is_never_a_match(self):
        result = classify_pair(
            pub("W1", "Contributors", doi="10.1/a"),
            pub("W2", "Contributors", doi="10.1/b"),
        )
        self.assertIsNone(result)


class CandidatePairsTest(unittest.TestCase):
    def test_shared_author_forms_a_pair(self):
        rows = [
            pub("W1", "Title one", authors=["A1"]),
            pub("W2", "Title two", authors=["A1"]),
        ]
        self.assertEqual(_candidate_pairs(rows), {("W1", "W2")})

    def test_prolific_author_bucket_is_skipped(self):
        # More publications than _MAX_PUBS_PER_AUTHOR share this author -
        # pairing all of them would be quadratic noise, not evidence.
        rows = [pub(f"W{i}", f"Distinct title number {i} about nothing in particular", authors=["A1"])
                for i in range(200)]
        self.assertEqual(_candidate_pairs(rows), set())

    def test_arxiv_id_shared_via_doi_forms_a_pair_even_without_shared_author(self):
        rows = [
            pub("W1", "Some paper", doi="10.48550/arxiv.2107.01278"),
            pub("W2", "Some paper, revised", doi="10.1088/1361-6463/ac2f16"),
        ]
        # No shared author/token blocking here; only the arXiv id in W1's DOI
        # points at the same work as W2's landing page.
        rows[1]["openalex_url"] = "https://arxiv.org/abs/2107.01278"
        self.assertEqual(_candidate_pairs(rows), {("W1", "W2")})


class FindVersionCandidatesTest(unittest.TestCase):
    class _StubClient:
        def __init__(self, rows):
            self._rows = rows

        def fetch_publications_for_dedup(self):
            return self._rows

    def test_report_rows_carry_status_held_and_a_mergeable_flag(self):
        client = self._StubClient([
            pub("W1", "Frequency Estimation of Multi-Sinusoidal Signals in Finite-Time",
                doi="10.48550/arxiv.2009.06400", type="preprint", pdate="2020-09-14",
                authors=["A1", "A2"]),
            pub("W2", "Finite Time Frequency Estimation for Multi-Sinusoidal Signals",
                doi="10.1016/j.ejcon.2021.01.004", pdate="2021-02-10", authors=["A1", "A2"]),
        ])
        report = find_version_candidates(client)
        self.assertEqual(len(report), 1)
        row = report[0]
        self.assertEqual(row["status"], "held")
        self.assertEqual(row["bucket"], "PREPRINT_PUBLISHED")
        self.assertTrue(row["mergeable"])

    def test_no_candidates_is_an_empty_report(self):
        client = self._StubClient([pub("W1", "A publication with no version anywhere near it")])
        self.assertEqual(find_version_candidates(client), [])


if __name__ == "__main__":
    unittest.main()
