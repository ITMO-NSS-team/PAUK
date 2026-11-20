import unittest
from datetime import date, timedelta
from unittest.mock import patch

from pauk.graph.version_candidates import (
    _ABSTRACT_MAX_REPEATED_TRIGRAM_SHARE,
    _ABSTRACT_MAX_WORD_SHARE,
    _ABSTRACT_MIN_CHARS,
    _ABSTRACT_MIN_DISTINCT_WORDS,
    _ABSTRACT_MIN_DIVERSITY,
    _ABSTRACT_MIN_WORDS,
    AUTO_CONFIRM_BUCKETS,
    AUTO_CONFIRM_POLICY,
    MERGE_BUCKETS,
    _abstract_evidence_text,
    _candidate_pairs,
    _is_preprint,
    _venue_kind,
    abstract_version_evidence,
    can_auto_confirm_version,
    classify_pair,
    find_version_candidates,
    route_medium_fuzzy,
)


def pub(pid, title, *, doi=None, journal=None, type="article",  # noqa: A002 (matches OpenAlex's own field name)
        year=None, pdate=None, abstract=None, authors=()):
    return {
        "id": pid, "title": title, "doi": doi, "journal": journal, "type": type,
        "year": year, "publication_date": pdate, "abstract": abstract, "pdf_urls": [],
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
        # "...84874" is the whole paper, "...84874.2" one of its revisions
        # with an editor's note appended to the title - still the paper's
        # title, unlike a figure's caption (see the component test below).
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

    def test_doi_component_with_its_own_caption_is_not_a_version(self):
        # Cifra LLC mints "<article DOI>.N" for each figure/table of the
        # article; the component's title is its caption.
        bucket, signals = classify_pair(
            pub("W1", "Снижение содержания афлатоксина М1 в молоке с помощью куркумина: кинетическое "
                      "моделирование методом Монте-Карло", doi="10.60797/jbg.2026.32.4", authors=["A1"]),
            pub("W2", "Input parameters and probability distributions used in the Monte Carlo simulation",
                doi="10.60797/jbg.2026.32.4.1", type="dataset", authors=["A1"]),
        )
        self.assertEqual(bucket, "SUPPLEMENT_OR_REVIEW")
        self.assertNotIn(bucket, MERGE_BUCKETS)
        self.assertEqual(signals["doi_b"], "10.60797/jbg.2026.32.4.1")

    def test_cyrillic_doi_component_with_a_similar_caption_is_not_a_version(self):
        # The caption reads like the paper's title (similarity ~0.83), but a
        # ".N" component typed as a deposit is never a revision.
        bucket, _signals = classify_pair(
            pub("W1", "КОМПЛЕКСНЫЙ ОБЗОР МУЛЬТИМОДАЛЬНЫХ ИНТЕРФЕЙСОВ И UX-ДИЗАЙНА ПРИЛОЖЕНИЙ",
                doi="10.60797/irj.2026.168.48", authors=["A1"]),
            pub("W2", "Классификация мультимодальных интерфейсов и UX-дизайна приложений",
                doi="10.60797/irj.2026.168.48.1", type="other", authors=["A1"]),
        )
        self.assertEqual(bucket, "SUPPLEMENT_OR_REVIEW")

    def test_erratum_suffix_is_not_a_version(self):
        bucket, _signals = classify_pair(
            pub("W1", "Multiple plane phase retrieval-inpainting with the saturated noisy data",
                type="conference-paper", authors=["A1", "A2"]),
            pub("W2", "Multiple plane phase retrieval-inpainting with the saturated noisy data (Erratum)",
                type="erratum", authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "ERRATUM")

    def test_retraction_notice_and_retracted_paper_are_not_a_version(self):
        bucket, _signals = classify_pair(
            pub("W1", "RETRACTED: Modeling of adsorptive removal of azithromycin from aquatic media",
                authors=["A1", "A2"]),
            pub("W2", 'Retraction notice to "Modeling of adsorptive removal of azithromycin from aquatic media"',
                type="retraction", authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "ERRATUM")

    def test_erratum_to_the_translated_edition_is_not_a_version(self):
        # The erratum follows the English edition's wording, the original
        # carries its own translation of the title - close, not literal.
        bucket, _signals = classify_pair(
            pub("W1", "Erratum to: Surface Emitting Quantum-Cascade Lasers with a Second-Order Grating "
                      "and Elevated Coefficient of Coupling", type="erratum", authors=["A1", "A2"]),
            pub("W2", "Surface emitting quantum-cascade lasers with a second-order grating and increased "
                      "coupling coefficient", journal="Известия Российской академии наук",
                authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "ERRATUM")

    def test_paper_titled_correction_of_is_not_a_notice(self):
        bucket, _signals = classify_pair(
            pub("W1", "Correction of chromatic aberration in wide-angle refractive imaging lenses",
                pdate="2020-01-01", authors=["A1", "A2"]),
            pub("W2", "Correction of chromatic aberrations in wide-angle refractive imaging lens systems",
                pdate="2020-06-01", authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "STRONG_FUZZY")

    def test_section_numbered_series_is_not_a_version(self):
        bucket, _signals = classify_pair(
            pub("W1", "Disturbance Observers: Methods and Applications. I. Methods", authors=["A1", "A2"]),
            pub("W2", "Disturbance Observers: Methods and Applications. II. Applications", authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "SERIES_NOT_VERSION")

    def test_bare_section_number_and_part_number_are_one_series(self):
        bucket, _signals = classify_pair(
            pub("W1", "Molecular transfer processes in liquid nutrient media in the yeast and beer industries. "
                      "2. Study of the rheological properties", authors=["A1", "A2"]),
            pub("W2", "Molecular transport processes in liquid nutrient media of yeast and beer industries. "
                      "Part 3. Thermophysical properties", authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "SERIES_NOT_VERSION")

    def test_part_numbers_of_unrelated_series_are_not_a_series(self):
        result = classify_pair(
            pub("W1", "State of the art and prospects for refrigerating compressor industry. "
                      "Part 2. Technology and science", authors=["A1"]),
            pub("W2", "Human Colour Perception Mechanisms: A Review of Natural Science Concepts. Part 1",
                authors=["A1"]),
        )
        self.assertIsNone(result)

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

    def test_russian_duplicates_are_compared_on_their_cyrillic_titles(self):
        bucket, _signals = classify_pair(
            pub("W1", "Исследование устойчивости нелинейных систем управления",
                doi="10.1/a", pdate="2021-01-01", authors=["A1", "A2"]),
            pub("W2", "Исследование устойчивости нелинейных систем автоматического управления",
                doi="10.1/b", pdate="2021-06-01", authors=["A1", "A2"]),
        )
        self.assertIn(bucket, MERGE_BUCKETS)

    def test_different_russian_titles_are_not_matched(self):
        result = classify_pair(
            pub("W1", "Исследование устойчивости нелинейных систем управления", doi="10.1/a", authors=["A1"]),
            pub("W2", "Методы оптимизации маршрутов беспилотных летательных аппаратов", doi="10.1/b",
                authors=["A1"]),
        )
        self.assertIsNone(result)

    def test_title_in_another_script_is_not_matched_on_title_alone(self):
        # Titles are compared as text, not translated: a Russian title and
        # its English rendering share no letters to compare.
        result = classify_pair(
            pub("W1", "Исследование устойчивости нелинейных систем управления",
                journal="Известия вузов. Приборостроение", authors=["A1"]),
            pub("W2", "Stability study of nonlinear control systems",
                journal="Journal of Instrument Engineering", authors=["A1"]),
        )
        self.assertIsNone(result)

    def test_cyrillic_lookalike_inside_a_latin_word_is_folded(self):
        bucket, _signals = classify_pair(
            pub("W1", "Robust nonlinear сontrol of quadrotor formations", doi="10.1/a", authors=["A1"]),
            pub("W2", "Robust nonlinear control of quadrotor formations", doi="10.1/b", authors=["A1"]),
        )
        self.assertEqual(bucket, "MISSED_BY_NORM")

    def test_journal_name_containing_osf_or_hal_is_not_a_preprint_venue(self):
        for journal in ("Journal of Biosfera", "The Marshal Journal"):
            with self.subTest(journal=journal):
                bucket, signals = classify_pair(
                    pub("W1", "Adaptive observers for uncertain nonlinear systems with delays",
                        doi="10.1/a", journal=journal, pdate="2020-01-01", authors=["A1", "A2"]),
                    pub("W2", "Adaptive observers for uncertain nonlinear systems with time delays",
                        doi="10.1/b", journal="Automatica", pdate="2020-09-01", authors=["A1", "A2"]),
                )
                self.assertFalse(signals["preprint_split"])
                self.assertNotEqual(bucket, "PREPRINT_PUBLISHED")

    def test_short_notice_subject_is_not_matched_by_containment(self):
        result = classify_pair(
            pub("W1", "Correction to: Introduction", type="erratum", authors=["A1"]),
            pub("W2", "Introduction to deep learning methods for spectroscopy", authors=["A1"]),
        )
        self.assertIsNone(result)

    def test_notice_subject_matches_only_on_word_boundaries(self):
        result = classify_pair(
            pub("W1", "Erratum: Graph networks traffic prediction", authors=["A1"]),
            pub("W2", "Hypergraph networks traffic prediction for urban mobility under sparse sensor coverage",
                authors=["A1"]),
        )
        self.assertIsNone(result)

    def test_paper_titled_correction_for_is_not_a_notice(self):
        bucket, _signals = classify_pair(
            pub("W1", "Correction for motion artifacts in functional MRI data",
                doi="10.1/a", pdate="2020-01-01", authors=["A1", "A2"]),
            pub("W2", "Correction for motion artifacts in functional MRI data using deep networks",
                doi="10.1/b", pdate="2020-06-01", authors=["A1", "A2"]),
        )
        self.assertIn(bucket, MERGE_BUCKETS)

    def test_hyphenated_correction_title_is_not_a_notice(self):
        bucket, _signals = classify_pair(
            pub("W1", "Correction-based approach to robust state estimation in power grids",
                doi="10.1/a", pdate="2020-01-01", authors=["A1", "A2"]),
            pub("W2", "Correction-based approach to robust state estimation for power grids",
                doi="10.1/b", pdate="2020-06-01", authors=["A1", "A2"]),
        )
        self.assertIn(bucket, MERGE_BUCKETS)

    def test_erratum_for_with_a_colon_is_a_notice(self):
        bucket, signals = classify_pair(
            pub("W1", "Animal model of assessing cerebrovascular functional reserve"),
            pub("W2", "Erratum for: Animal model of assessing cerebrovascular functional reserve"),
        )
        self.assertEqual(bucket, "ERRATUM")
        self.assertEqual(signals["original"], "W1")

    def test_trailing_number_on_one_title_only_is_not_a_series(self):
        bucket, _signals = classify_pair(
            pub("W1", "Spread forecasting model for the pandemic of COVID-19", doi="10.1101/2020.04.01.1",
                journal="medRxiv", type="preprint", pdate="2020-04-01", authors=["A1"]),
            pub("W2", "Spread forecasting model for the pandemic of COVID", doi="10.1/b",
                journal="Mathematics", pdate="2021-01-01", authors=["A1"]),
        )
        self.assertEqual(bucket, "PREPRINT_PUBLISHED")

    def test_series_number_after_hash_mid_title(self):
        bucket, _signals = classify_pair(
            pub("W1", "Lecture notes #2 on optimal control theory", doi="10.1/a", authors=["A1", "A2"]),
            pub("W2", "Lecture notes #3 on optimal control theory", doi="10.1/b", authors=["A1", "A2"]),
        )
        self.assertEqual(bucket, "SERIES_NOT_VERSION")

    def test_software_releases_with_one_title_are_not_missed_by_norm(self):
        result = classify_pair(
            pub("W1", "PAUK publication graph toolkit release", doi="10.5281/zenodo.1", type="software"),
            pub("W2", "PAUK publication graph toolkit release", doi="10.5281/zenodo.2", type="software"),
        )
        self.assertIsNone(result)

    def test_generic_front_matter_title_is_never_a_match(self):
        result = classify_pair(
            pub("W1", "Contributors", doi="10.1/a"),
            pub("W2", "Contributors", doi="10.1/b"),
        )
        self.assertIsNone(result)


_RETINAL_ABSTRACT = (
    "We evaluate retinal image segmentation using a neural network trained on 240 annotated scans. "
    "Independent validation measures lesion boundaries, sensitivity, and calibration across three clinical cohorts. "
    "The proposed method reduces false positives without removing small vessels and remains stable under changes "
    "in illumination."
)
_BRAIN_ABSTRACT = (
    "We investigate brain tumor classification from magnetic resonance images acquired in 180 patients. "
    "A convolutional model distinguishes glioma grades using volumetric texture features and regional tissue contrast. "
    "External evaluation estimates diagnostic accuracy, uncertainty, and robustness against scanner variation "
    "without analyzing retinal vessels."
)
_LEGAL_BOILERPLATE = (
    "© 2024 The Author(s). All rights reserved. This is an open access article distributed under the terms of the "
    "Creative Commons Attribution License, which permits unrestricted use, distribution, and reproduction in any "
    "medium, provided the original author and source are credited."
)


_CC_BY_BOILERPLATE = (
    "© The Author(s) 2024. This article is licensed under a Creative Commons Attribution 4.0 International License, "
    "which permits use, sharing, adaptation, distribution and reproduction in any medium or format, as long as you "
    "give appropriate credit to the original author(s) and the source, provide a link to the Creative Commons licence, "
    "and indicate if changes were made. The images or other third party material in this article are included in the "
    "article's Creative Commons licence, unless indicated otherwise in a credit line to the material. If material is "
    "not included in the article's Creative Commons licence and your intended use is not permitted by statutory "
    "regulation or exceeds the permitted use, you will need to obtain permission directly from the copyright holder. "
    "To view a copy of this licence, visit http://creativecommons.org/licenses/by/4.0/. "
    "Springer Nature remains neutral with regard to jurisdictional claims in published maps and institutional affiliations."
)


class AutoConfirmationPolicyTest(unittest.TestCase):
    def setUp(self):
        self.a = pub("W1", "Robust neural network classification method",
                     doi="10.1/a", pdate="2024-01-01", authors=["A1", "A2"], abstract=_RETINAL_ABSTRACT)
        self.b = pub("W2", self.a["title"], doi="10.1/b", pdate="2024-06-01", authors=["A1", "A2"],
                     abstract=_RETINAL_ABSTRACT)

    def test_whitelist_is_explicit_and_requires_version_candidates(self):
        self.assertEqual(AUTO_CONFIRM_BUCKETS, {
            "MISSED_BY_NORM", "DOI_SIBLING", "PREPRINT_PUBLISHED", "RU_EN_TRANSLATION", "STRONG_FUZZY",
        })
        self.assertLessEqual(AUTO_CONFIRM_BUCKETS, MERGE_BUCKETS)
        for bucket in AUTO_CONFIRM_BUCKETS:
            with self.subTest(bucket=bucket):
                self.assertTrue(can_auto_confirm_version(self.a, self.b, bucket))

    def test_medium_family_and_look_alikes_are_never_auto_confirmed(self):
        for bucket in ("MEDIUM_FUZZY", "MEDIUM_FUZZY_STRONG", "MEDIUM_FUZZY_LLM_SAME",
                       "MEDIUM_FUZZY_CONF_JOURNAL", "MEDIUM_FUZZY_RELATED", "ERRATUM",
                       "COVER", "SUPPLEMENT_OR_REVIEW", "SERIES_NOT_VERSION", "NEW_BUCKET"):
            with self.subTest(bucket=bucket):
                self.assertFalse(can_auto_confirm_version(self.a, self.b, bucket))

    def test_known_dates_are_required_except_for_doi_siblings(self):
        for bucket in AUTO_CONFIRM_BUCKETS:
            with self.subTest(bucket=bucket):
                self.assertEqual(can_auto_confirm_version({**self.a, "publication_date": None}, self.b, bucket),
                                 bucket == "DOI_SIBLING")
        self.assertTrue(can_auto_confirm_version(
            {**self.a, "publication_date": None, "year": 2024},
            {**self.b, "publication_date": "invalid", "year": 2025}, "MISSED_BY_NORM"))

    def test_author_ids_not_names_must_corroborate_the_pair(self):
        for authors in ([], [{"name": "A1"}], [{"person_id": "someone_else", "name": "A1"}]):
            for bucket in AUTO_CONFIRM_BUCKETS:
                with self.subTest(authors=authors, bucket=bucket):
                    self.assertFalse(can_auto_confirm_version(self.a, {**self.b, "authors": authors}, bucket))

    def test_half_author_overlap_is_the_boundary_for_fuzzy_and_normalized_titles(self):
        half = [{"person_id": "A1"}]
        weak = [{"person_id": pid} for pid in ("A1", "A3", "A4")]
        for bucket in ("MISSED_BY_NORM", "RU_EN_TRANSLATION", "STRONG_FUZZY"):
            with self.subTest(bucket=bucket):
                self.assertTrue(can_auto_confirm_version(self.a, {**self.b, "authors": half}, bucket))
                self.assertFalse(can_auto_confirm_version(self.a, {**self.b, "authors": weak}, bucket))
        self.assertTrue(can_auto_confirm_version(self.a, {**self.b, "authors": weak}, "DOI_SIBLING"))

    def test_date_gap_boundaries(self):
        for bucket in AUTO_CONFIRM_BUCKETS - {"DOI_SIBLING"}:
            limit = 1095 if bucket == "PREPRINT_PUBLISHED" else 1100
            for days in (limit, limit + 1):
                with self.subTest(bucket=bucket, days=days):
                    later = {**self.b, "publication_date": str(date(2024, 1, 1) + timedelta(days=days))}
                    self.assertEqual(can_auto_confirm_version(self.a, later, bucket), days == limit)

    def test_translation_requires_unrounded_strong_title_similarity(self):
        for similarity in (0.8996, 0.9):
            with self.subTest(similarity=similarity), patch(
                "pauk.graph.version_candidates._title_similarity", return_value=similarity,
            ):
                self.assertEqual(can_auto_confirm_version(self.a, self.b, "RU_EN_TRANSLATION"), similarity >= 0.9)


class AbstractVersionEvidenceTest(unittest.TestCase):
    def evidence(self, a, b):
        return abstract_version_evidence({"abstract": a}, {"abstract": b})

    def quality(self, text):
        return _abstract_evidence_text(text)[1]

    def words(self, count):
        return [f"boundary{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(count)]

    def test_quality_floors_are_explicit_engineering_safeguards(self):
        self.assertEqual((_ABSTRACT_MIN_CHARS, _ABSTRACT_MIN_WORDS, _ABSTRACT_MIN_DISTINCT_WORDS), (160, 24, 16))
        self.assertEqual((_ABSTRACT_MIN_DIVERSITY, _ABSTRACT_MAX_WORD_SHARE,
                          _ABSTRACT_MAX_REPEATED_TRIGRAM_SHARE), (0.5, 0.15, 0.25))
        self.assertEqual(AUTO_CONFIRM_POLICY, "rules-v3")
        self.assertEqual(self.quality(_RETINAL_ABSTRACT), "substantive")

    def test_character_floor_boundary(self):
        words = [word[-2:] for word in self.words(_ABSTRACT_MIN_WORDS)]
        text = " ".join(words)
        for length in (_ABSTRACT_MIN_CHARS - 1, _ABSTRACT_MIN_CHARS):
            with self.subTest(length=length):
                padded = "x" * (length - len(text)) + text
                self.assertEqual(len(padded), length)
                self.assertEqual(self.quality(padded), "substantive" if length == _ABSTRACT_MIN_CHARS else "too_short")

    def test_word_floor_boundary_and_numeric_padding(self):
        for count in (_ABSTRACT_MIN_WORDS - 1, _ABSTRACT_MIN_WORDS):
            with self.subTest(count=count):
                text = " ".join(self.words(count))
                self.assertEqual(self.quality(text), "substantive" if count == _ABSTRACT_MIN_WORDS else "too_few_words")
        text = "A short scientific result. " + " ".join(str(i) for i in range(300))
        self.assertEqual(self.quality(text), "too_few_words")

    def test_distinct_word_floor_boundary(self):
        for count in (_ABSTRACT_MIN_DISTINCT_WORDS - 1, _ABSTRACT_MIN_DISTINCT_WORDS):
            with self.subTest(count=count):
                words = self.words(count)
                text = " ".join(words + list(reversed(words[:_ABSTRACT_MIN_WORDS - count])))
                self.assertEqual(self.quality(text),
                                 "substantive" if count == _ABSTRACT_MIN_DISTINCT_WORDS else "too_few_distinct_words")

    def test_diversity_floor_boundary(self):
        words = self.words(_ABSTRACT_MIN_DISTINCT_WORDS)
        boundary = " ".join(words + list(reversed(words)))
        self.assertEqual(self.quality(boundary), "substantive")
        self.assertEqual(self.quality(boundary + " " + words[0]), "low_lexical_diversity")

    def test_repeated_word_share_boundary(self):
        for repeats in (6, 7):
            with self.subTest(repeats=repeats):
                text = " ".join(["padding"] * repeats + self.words(40 - repeats))
                self.assertEqual(self.quality(text), "substantive" if repeats == 6 else "repeated_word_padding")

    def test_repeated_trigram_share_boundary(self):
        words = self.words(28)
        for middle in (words[6:], words[6:-1]):
            with self.subTest(middle=len(middle)):
                text = " ".join(words[:6] + middle + words[:6])
                self.assertEqual(self.quality(text), "substantive" if len(middle) == 22 else "repeated_phrase_padding")

    def test_missing_short_legal_and_placeholder_content_is_unknown_not_matching(self):
        for text in (None, "", " \n ", "We present a classification method.", _LEGAL_BOILERPLATE, _CC_BY_BOILERPLATE,
                     "No abstract available. " * 30, "Abstract is unavailable. " * 30,
                     "This paper has no abstract. " * 30,
                     "© 2024 Elsevier B.V. All rights reserved. " * 20,
                     "This work is licensed under a Creative Commons Attribution 4.0 International License. " * 10,
                     _LEGAL_BOILERPLATE + "Retinal results are promising.",
                     "<p>" + _LEGAL_BOILERPLATE.replace(" ", "\n") + "</p>"):
            with self.subTest(text=text):
                evidence = self.evidence(text, text)
                self.assertEqual(evidence["abstract_evidence"], "unknown")
                self.assertIn("a:", evidence["abstract_evidence_reason"])
        self.assertEqual(self.evidence(_RETINAL_ABSTRACT, None)["abstract_evidence"], "unknown")
        self.assertEqual(self.evidence(None, _RETINAL_ABSTRACT)["abstract_evidence"], "unknown")

    def test_repeated_padding_cannot_make_a_short_result_substantive(self):
        text = "Retinal results are promising. " + "Method improves image analysis. " * 100
        self.assertNotEqual(self.quality(text), "substantive")
        self.assertEqual(self.evidence(text, text)["abstract_evidence"], "unknown")

    def test_unicode_html_and_whitespace_formatting_preserving_case_are_positive(self):
        text = _RETINAL_ABSTRACT + " Café measurements are reproducible."
        formatted = "<p>" + text.replace("Café", "Café").replace(
            "neural network", "<em>neural&nbsp;network</em>").replace(". ", ".</p>\n<p>") + "</p>"
        self.assertEqual(self.evidence(text, formatted)["abstract_evidence"], "match")
        self.assertEqual(self.evidence(text + " Salt & water.", text + " Salt &amp; water.")["abstract_evidence"], "match")

    def test_uppercase_substantive_text_is_deliberately_not_identity_evidence(self):
        self.assertEqual(self.evidence(_RETINAL_ABSTRACT, _RETINAL_ABSTRACT.upper())["abstract_evidence"], "mismatch")
        self.assertEqual(self.quality(_RETINAL_ABSTRACT.upper()), "substantive")

    def test_quality_counting_remains_case_insensitive(self):
        text = " ".join(["padding", "PADDING", "Padding", "pAdDiNg", "paDDing", "paddiNG", "PAddING"] + self.words(33))
        self.assertEqual(self.quality(text), "repeated_word_padding")
        self.assertEqual(self.quality(_LEGAL_BOILERPLATE.upper() + "No abstract available. " * 30),
                         "boilerplate_or_placeholder")

    def test_known_boilerplate_is_removed_before_equality_and_quality_checks(self):
        self.assertEqual(self.evidence(_RETINAL_ABSTRACT, _RETINAL_ABSTRACT + " " + _LEGAL_BOILERPLATE)
                         ["abstract_evidence"], "match")
        self.assertEqual(self.evidence(_RETINAL_ABSTRACT, _RETINAL_ABSTRACT + " " + _CC_BY_BOILERPLATE)
                         ["abstract_evidence"], "match")
        self.assertEqual(self.evidence(_RETINAL_ABSTRACT, _RETINAL_ABSTRACT + " " + _CC_BY_BOILERPLATE.upper())
                         ["abstract_evidence"], "match")
        evidence = self.evidence(_LEGAL_BOILERPLATE * 40 + _RETINAL_ABSTRACT,
                                 _LEGAL_BOILERPLATE * 40 + _BRAIN_ABSTRACT)
        self.assertEqual(evidence["abstract_evidence"], "mismatch")
        self.assertEqual(evidence["abstract_evidence_reason"], "different_substantive_text")

    def test_numbers_units_negation_punctuation_and_order_remain_distinguishable(self):
        for other in (_RETINAL_ABSTRACT.replace("240", "241"),
                      _RETINAL_ABSTRACT.replace("without removing", "with removing"),
                      _RETINAL_ABSTRACT.replace("sensitivity, and calibration", "calibration, and sensitivity"),
                      _RETINAL_ABSTRACT.replace("240", "24.0")):
            with self.subTest(other=other):
                self.assertEqual(self.evidence(_RETINAL_ABSTRACT, other)["abstract_evidence"], "mismatch")
        for a, b in (("5 mg", "5 g"), ("5 mM", "5 mm"), ("5 μg", "5 mg"),
                     ("5 m²", "5 m2"), ("p < 0.05", "p > 0.05")):
            with self.subTest(a=a, b=b):
                self.assertEqual(self.evidence(_RETINAL_ABSTRACT + " " + a,
                                               _RETINAL_ABSTRACT + " " + b)["abstract_evidence"], "mismatch")


class ClassifiedAcceptanceTest(unittest.TestCase):
    def pairs(self, abstract_a=_RETINAL_ABSTRACT, abstract_b=_RETINAL_ABSTRACT):
        cases = (
            ("MISSED_BY_NORM", "Robust neural network classification method",
             "Robust neural network classification method", {}, {}),
            ("DOI_SIBLING", "Expanding the stdpopsim species catalog",
             "Expanding the stdpopsim species catalog: revision 2", {"doi": "10.7554/elife.84874"},
             {"doi": "10.7554/elife.84874.2"}),
            ("PREPRINT_PUBLISHED", "Frequency Estimation of Multi-Sinusoidal Signals in Finite-Time",
             "Finite Time Frequency Estimation for Multi-Sinusoidal Signals",
             {"doi": "10.48550/arxiv.2009.06400", "journal": "arXiv", "type": "preprint"}, {}),
            ("RU_EN_TRANSLATION", "Impact of transverse optical confinment on performance of VCSELs",
             "Impact of Transverse Optical Confinement on Performance of VCSELs",
             {"journal": "Письма в журнал технической физики"}, {"journal": "Technical Physics Letters"}),
            ("STRONG_FUZZY", "Improvement of methods and means of thermal imagers verification and calibration",
             "Improvement of Methods and Means for the Verification and Calibration of Thermal Imagers", {}, {}),
        )
        for bucket, title_a, title_b, options_a, options_b in cases:
            options_a = {"doi": "10.1/a", "pdate": "2024-01-01", **options_a}
            options_b = {"doi": "10.1/b", "pdate": "2024-06-01", **options_b}
            yield bucket, pub("W1", title_a, authors=["A1", "A2"], abstract=abstract_a, **options_a), pub(
                "W2", title_b, authors=["A1", "A2"], abstract=abstract_b, **options_b)

    def test_real_classification_and_acceptance_positive_paths_for_every_auto_bucket(self):
        text = _RETINAL_ABSTRACT + " Café conductivity is 5 mS/cm & reproducible."
        formatted = "<p>" + text.replace("Café", "Café").replace(" ", "&nbsp;") + "</p>"
        for expected, a, b in self.pairs(abstract_a=text, abstract_b=formatted):
            with self.subTest(bucket=expected):
                bucket, signals = classify_pair(a, b)
                self.assertEqual(bucket, expected)
                self.assertEqual(signals["abstract_evidence"], "match")
                self.assertTrue(can_auto_confirm_version(a, b, bucket))

    def test_uppercase_only_changes_hold_every_auto_bucket(self):
        for expected, a, b in self.pairs(abstract_b=_RETINAL_ABSTRACT.upper()):
            with self.subTest(bucket=expected):
                bucket, signals = classify_pair(a, b)
                self.assertEqual(bucket, expected)
                self.assertEqual(signals["abstract_evidence"], "mismatch")
                self.assertFalse(can_auto_confirm_version(a, b, bucket))

    def test_all_scientific_case_and_meaning_changes_hold_actual_classified_candidates(self):
        scientific_changes = (
            ("5 mS", "5 MS"), ("5 mGy", "5 MGy"),
            ("5 mS/cm", "5 MS/cm"), ("5 mGy/s", "5 MGy/s"),
            ("mS", "MS"), ("mGy", "MGy"), ("mS/cm", "MS/cm"),
            ("5 qz", "5 QZ"), ("5 mΩ", "5 MΩ"), ("5 μS", "5 μs"),
            ("Co", "CO"), ("NO", "No"), ("Fe₂O₃", "FE₂O₃"), ("pH", "PH"),
            ("α state", "Α state"), ("DNA", "dna"),
            ("5e-3 mS", "5e-3 MS"), ("5e-3 mS", "5e+3 mS"), ("1e-3", "1E-3"),
            ("5 mg", "5 g"), ("5 mM", "5 mm"), ("5 μg", "5 mg"),
            ("5 m²", "5 m2"), ("p < 0.05", "p > 0.05"),
            ("H<sub>2</sub>O", "H2O"), ("m<sup>2</sup>", "m2"),
            ("without removing noise", "with removing noise"),
            ("sensitivity, and calibration", "calibration, and sensitivity"), ("240", "241"), ("240", "24.0"),
        )
        for first, second in scientific_changes:
            for expected, a, b in self.pairs(_RETINAL_ABSTRACT + " " + first, _RETINAL_ABSTRACT + " " + second):
                with self.subTest(bucket=expected, first=first, second=second):
                    bucket, signals = classify_pair(a, b)
                    self.assertEqual(bucket, expected)
                    self.assertIn(bucket, MERGE_BUCKETS)
                    self.assertEqual(signals["abstract_evidence"], "mismatch")
                    self.assertFalse(can_auto_confirm_version(a, b, bucket))

    def test_real_classification_and_acceptance_holds_bad_content_in_every_auto_bucket(self):
        for abstract_a, abstract_b in ((_RETINAL_ABSTRACT, _BRAIN_ABSTRACT), (None, None),
                                       (_RETINAL_ABSTRACT, None), (None, _RETINAL_ABSTRACT),
                                       ("We classify retinal images.", "We classify retinal images."),
                                       (_LEGAL_BOILERPLATE, _LEGAL_BOILERPLATE),
                                       (_CC_BY_BOILERPLATE, _CC_BY_BOILERPLATE),
                                       (_CC_BY_BOILERPLATE * 10 + _RETINAL_ABSTRACT,
                                        _CC_BY_BOILERPLATE * 10 + _BRAIN_ABSTRACT),
                                       ("No abstract available. " * 30, "No abstract available. " * 30)):
            for expected, a, b in self.pairs(abstract_a, abstract_b):
                with self.subTest(bucket=expected, abstract_a=abstract_a, abstract_b=abstract_b):
                    bucket, signals = classify_pair(a, b)
                    self.assertEqual(bucket, expected)
                    self.assertIn(bucket, MERGE_BUCKETS)
                    self.assertNotEqual(signals["abstract_evidence"], "match")
                    self.assertIn("abstract_evidence_reason", signals)
                    self.assertFalse(can_auto_confirm_version(a, b, bucket))

    def test_exact_title_retinal_and_brain_tumor_papers_remain_candidates_but_not_auto_same(self):
        expected, a, b = next(self.pairs(_RETINAL_ABSTRACT, _BRAIN_ABSTRACT))
        bucket, signals = classify_pair(a, b)
        self.assertEqual(bucket, expected)
        self.assertEqual(bucket, "MISSED_BY_NORM")
        self.assertEqual(signals["abstract_evidence"], "mismatch")
        self.assertFalse(can_auto_confirm_version(a, b, bucket))

    def test_report_includes_abstract_outcome_and_reason_for_early_buckets(self):
        for expected, a, b in list(self.pairs())[:2]:
            with self.subTest(bucket=expected):
                report = find_version_candidates(None, rows=[a, b])
                self.assertEqual(len(report), 1)
                self.assertEqual(report[0]["bucket"], expected)
                self.assertEqual(report[0]["abstract_evidence"], "match")
                self.assertEqual(report[0]["abstract_evidence_reason"], "equal_substantive_text")

    def test_two_conference_venues_stay_manual_despite_reused_abstract(self):
        for bucket, a, b in self.pairs():
            if bucket == "PREPRINT_PUBLISHED":
                continue
            for a_venue, b_venue in (({"journal": None, "venue_type": "conference"},
                                      {"journal": None, "venue_type": "conference"}),
                                     ({"journal": "Journal of Physics: Conference Series", "venue_type": "journal"},
                                      {"journal": "2024 Neural Imaging Symposium", "venue_type": "journal"})):
                with self.subTest(bucket=bucket, venues=(a_venue, b_venue)):
                    first, second = {**a, **a_venue}, {**b, **b_venue}
                    actual, signals = classify_pair(first, second)
                    self.assertIn(actual, AUTO_CONFIRM_BUCKETS)
                    self.assertEqual((signals["venue_kind_a"], signals["venue_kind_b"]), ("conference", "conference"))
                    self.assertFalse(can_auto_confirm_version(first, second, actual))
        bucket, a, b = next(self.pairs())
        a = {**a, "journal": "Imaging Symposium", "versions": [{"doi": b["doi"]}]}
        b = {**b, "journal": "Vision Conference"}
        actual, signals = classify_pair(a, b)
        self.assertEqual(actual, bucket)
        self.assertEqual(signals["abstract_evidence"], "match")
        self.assertFalse(can_auto_confirm_version(a, b, actual))
        self.assertTrue(can_auto_confirm_version(a, {**b, "journal": "Imaging Journal"}, actual))

    def test_explicit_conference_types_prevent_auto_same_with_acronym_venues(self):
        _, a, b = next(self.pairs())
        for first_type in ("conference-paper", "proceedings-article"):
            for second_type in ("conference-paper", "proceedings-article"):
                for venue_type in (None, "journal", "conference"):
                    with self.subTest(first_type=first_type, second_type=second_type, venue_type=venue_type):
                        first = {**a, "journal": "ICRA", "type": first_type, "venue_type": venue_type}
                        second = {**b, "journal": "ICCV", "type": second_type, "venue_type": "journal"}
                        bucket, signals = classify_pair(first, second)
                        self.assertEqual(bucket, "MISSED_BY_NORM")
                        self.assertEqual(signals["abstract_evidence"], "match")
                        self.assertEqual((signals["venue_kind_a"], signals["venue_kind_b"]),
                                         ("conference", "conference"))
                        self.assertFalse(can_auto_confirm_version(first, second, bucket))

    def test_preprint_exclusion_outranks_conflicting_conference_metadata(self):
        for expected, a, b in self.pairs():
            if expected != "PREPRINT_PUBLISHED":
                continue
            first = {**a, "venue_type": "conference"}
            second = {**b, "journal": "ICRA", "type": "conference-paper", "venue_type": "journal"}
            bucket, signals = classify_pair(first, second)
            self.assertEqual(bucket, expected)
            self.assertEqual((signals["venue_kind_a"], signals["venue_kind_b"]), (None, "conference"))
            self.assertTrue(can_auto_confirm_version(first, second, bucket))

    def test_normalization_is_cached_and_reused_by_classification_and_policy(self):
        _abstract_evidence_text.cache_clear()
        bucket, a, b = next(self.pairs())
        result = classify_pair(a, b)
        misses = _abstract_evidence_text.cache_info().misses
        self.assertEqual(result[0], bucket)
        self.assertEqual(misses, 1)
        self.assertTrue(can_auto_confirm_version(a, b, bucket))
        self.assertEqual(_abstract_evidence_text.cache_info().misses, misses)
        b["abstract"] = _BRAIN_ABSTRACT
        self.assertFalse(can_auto_confirm_version(a, b, bucket))
        self.assertEqual(_abstract_evidence_text.cache_info().misses, misses + 1)

    def test_content_work_is_not_moved_before_cheap_candidate_rejection(self):
        with patch("pauk.graph.version_candidates.abstract_version_evidence", side_effect=AssertionError("expensive")):
            self.assertIsNone(classify_pair(
                pub("W1", "Optical properties of icosahedral quasicrystals", abstract=_RETINAL_ABSTRACT),
                pub("W2", "Survey of protein structure prediction algorithms", abstract=_RETINAL_ABSTRACT)))
            self.assertIsNone(classify_pair(
                pub("W1", "Improvement of methods and means of thermal imagers verification and calibration"),
                pub("W2", "Improvement of Methods and Means for the Verification and Calibration of Thermal Imagers")))


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


    def test_prolific_author_still_pairs_works_sharing_rare_words(self):
        rows = [pub(f"T{i}", f"Distinct title number {i} about nothing in particular", authors=["A1"])
                for i in range(120)]
        rows += [
            pub("WA", "Quadrotor formation control under wind disturbances", authors=["A1"]),
            pub("WB", "Quadrotor formation control with wind disturbance rejection", authors=["A1"]),
        ]
        # The shared words are too common across the graph for title-token
        # blocking, so only the author's own bucket can bring these together.
        rows += [pub(f"O{i}", f"Quadrotor formation control wind study {i}", authors=[f"B{i}"])
                 for i in range(50)]
        self.assertIn(("WA", "WB"), _candidate_pairs(rows))

    def test_arxiv_id_in_any_pdf_url_forms_a_pair(self):
        rows = [
            pub("W1", "Some paper", doi="10.48550/arxiv.2107.01278"),
            pub("W2", "Some paper, revised", doi="10.1088/1361-6463/ac2f16"),
        ]
        rows[1]["pdf_urls"] = ["https://example.org/a.pdf", "https://arxiv.org/pdf/2107.01278"]
        self.assertEqual(_candidate_pairs(rows), {("W1", "W2")})


class CoverTest(unittest.TestCase):
    article = pub("W1", "Hydrogel Actuators by Autocatalytic Reaction Networks",
                  doi="10.1002/adma.1", journal="Advanced Materials", authors=["A1", "A2"])

    def cover(self, title, **kwargs):
        return pub("W2", title, doi="10.1002/adma.2", authors=["A1", "A2"], **kwargs)

    def test_cover_markers_are_their_own_bucket(self):
        for title in ("Front Cover: Hydrogel Actuators by Autocatalytic Reaction Networks",
                      "Cover Feature: Hydrogel Actuators by Autocatalytic Reaction Networks",
                      "Hydrogel Actuators by Autocatalytic Reaction Networks (Adv. Mater. 13/2022)"):
            with self.subTest(title=title):
                bucket, signals = classify_pair(self.article, self.cover(title))
                self.assertEqual(bucket, "COVER")
                self.assertEqual((signals["cover"], signals["article"]), ("W2", "W1"))
                self.assertNotIn(bucket, MERGE_BUCKETS)

    def test_paratext_with_the_articles_title_is_a_cover(self):
        bucket, _ = classify_pair(self.article, self.cover(self.article["title"], type="paratext"))
        self.assertEqual(bucket, "COVER")

    def test_a_rubric_counts_only_in_the_articles_own_journal(self):
        title = "Polymer Coatings: Hydrogel Actuators by Autocatalytic Reaction Networks"
        bucket, _ = classify_pair(self.article, self.cover(title, journal="Advanced Materials"))
        self.assertEqual(bucket, "COVER")
        result = classify_pair(self.article, self.cover(title, journal="Soft Matter"))
        self.assertNotEqual(result and result[0], "COVER")

    def test_leading_publishers_note_is_an_erratum(self):
        bucket, _ = classify_pair(
            pub("W1", "Alpha-MoO3 SiC metasurface for tunable infrared emission", doi="10.1/a"),
            pub("W2", "Publisher's Note: \u201cAlpha-MoO3 SiC metasurface for tunable infrared emission\u201d",
                doi="10.1/b"))
        self.assertEqual(bucket, "ERRATUM")


class VenueKindTest(unittest.TestCase):
    def test_supported_conference_types_outrank_journal_metadata_and_name_fallback(self):
        for publication_type in ("conference-paper", "proceedings-article", " CONFERENCE-PAPER "):
            for venue_type in (None, "journal", " JOURNAL ", "conference", "book series"):
                for name in (None, "ICRA", "ICCV"):
                    with self.subTest(publication_type=publication_type, venue_type=venue_type, name=name):
                        self.assertEqual(_venue_kind({"type": publication_type, "venue_type": venue_type,
                                                     "journal": name}), "conference")

    def test_repository_and_preprint_exclusions_have_highest_precedence(self):
        for metadata in ({"venue_type": "repository"}, {"venue_type": "preprint"},
                         {"journal": "arXiv"}, {"journal": "Zenodo Conference"},
                         {"type": "preprint"}, {"type": " PREPRINT "},
                         {"doi": "https://doi.org/10.48550/arxiv.2009.06400"}):
            with self.subTest(metadata=metadata):
                self.assertIsNone(_venue_kind({"journal": "ICRA", "venue_type": "conference",
                                               "type": "conference-paper", **metadata}))

    def test_conference_venues_outrank_article_types_and_explicit_journals(self):
        self.assertEqual(_venue_kind({"type": "article", "venue_type": "conference", "journal": "ICRA"}),
                         "conference")
        self.assertEqual(_venue_kind({"type": "journal-article", "venue_type": "journal",
                                      "journal": "Journal of Physics: Conference Series"}), "conference")

    def test_acronyms_and_unsupported_types_do_not_guess_a_conference(self):
        for name in ("ICRA", "ICCV", "Unknown Acronym"):
            with self.subTest(name=name):
                self.assertEqual(_venue_kind({"journal": name}), "journal")
                self.assertEqual(_venue_kind({"journal": name, "type": "conference-paper-appendix"}), "journal")
                self.assertIsNone(_venue_kind({"journal": name, "type": "article", "venue_type": "book"}))
        self.assertEqual(_venue_kind({"venue_type": "journal"}), "journal")
        self.assertIsNone(_venue_kind({}))


class RouteMediumFuzzyTest(unittest.TestCase):
    signals = {"abstract_sim": 0.03, "preprint_split": False,
               "venue_kind_a": "conference", "venue_kind_b": "journal"}

    def route(self, verdict=None, **overrides):
        judge = (lambda a, b: verdict) if verdict is not None else None
        return route_medium_fuzzy({}, {}, {**self.signals, **overrides}, judge)

    def test_shared_abstract_or_preprint_skips_the_llm(self):
        self.assertEqual(self.route(abstract_sim=0.5), "MEDIUM_FUZZY_STRONG")
        self.assertEqual(self.route(preprint_split=True), "MEDIUM_FUZZY_STRONG")

    def test_without_a_verdict_the_bucket_stays_whole(self):
        self.assertEqual(self.route(), "MEDIUM_FUZZY")

    def test_verdict_and_venues_split_the_rest(self):
        self.assertEqual(self.route({"same_work": True}), "MEDIUM_FUZZY_LLM_SAME")
        self.assertEqual(self.route({"same_work": False}), "MEDIUM_FUZZY_CONF_JOURNAL")
        self.assertEqual(self.route({"same_work": False}, venue_kind_a="journal"), "MEDIUM_FUZZY_RELATED")
        self.assertNotIn("MEDIUM_FUZZY_RELATED", MERGE_BUCKETS)

    def test_classified_medium_routes_honor_publication_types_and_venue_precedence(self):
        cases = (
            ({"type": "conference-paper", "journal": "ICRA"},
             {"type": "conference-paper", "journal": "ICCV"},
             ("conference", "conference"), "MEDIUM_FUZZY_RELATED"),
            ({"type": "conference-paper", "journal": "ICRA", "venue_type": "journal"},
             {"type": "proceedings-article", "journal": "ICCV", "venue_type": "journal"},
             ("conference", "conference"), "MEDIUM_FUZZY_RELATED"),
            ({"type": "conference-paper", "journal": "ICRA", "venue_type": "journal"},
             {"type": "article", "journal": "Nano Letters", "venue_type": "journal"},
             ("conference", "journal"), "MEDIUM_FUZZY_CONF_JOURNAL"),
            ({"type": "proceedings-article", "journal": "ICRA"},
             {"type": "article", "journal": "Nano Letters"},
             ("conference", "journal"), "MEDIUM_FUZZY_CONF_JOURNAL"),
            ({"type": "article", "journal": "ICRA", "venue_type": "conference"},
             {"type": "article", "journal": "Nano Letters", "venue_type": "journal"},
             ("conference", "journal"), "MEDIUM_FUZZY_CONF_JOURNAL"),
            ({"type": "conference-paper", "journal": "ICRA", "venue_type": "repository"},
             {"type": "article", "journal": "Nano Letters", "venue_type": "journal"},
             (None, "journal"), "MEDIUM_FUZZY_RELATED"),
            ({"type": "article", "journal": "ICRA"}, {"type": "article", "journal": "ICCV"},
             ("journal", "journal"), "MEDIUM_FUZZY_RELATED"),
        )
        for first, second, venue_kinds, expected in cases:
            with self.subTest(first=first, second=second):
                a = {**pub("W1", "Method of the Joint Clustering in Network and Correlation Spaces",
                           doi="10.1/a", pdate="2020-06-24", authors=["A1", "A2"]), **first}
                b = {**pub("W2", "Method for Joint Clustering in Graph and Correlation Spaces",
                           doi="10.1/b", pdate="2021-12-01", authors=["A1", "A2"]), **second}
                bucket, signals = classify_pair(a, b)
                self.assertEqual(bucket, "MEDIUM_FUZZY")
                self.assertEqual((signals["venue_kind_a"], signals["venue_kind_b"]), venue_kinds)
                routed = route_medium_fuzzy(a, b, signals, lambda a, b: {"same_work": False})
                self.assertEqual(routed, expected)
                self.assertFalse(can_auto_confirm_version(a, b, routed))
                report = find_version_candidates(None, rows=[a, b], judge=lambda a, b: {"same_work": False})
                self.assertEqual(report[0]["bucket"], expected)

    def test_proceedings_typed_as_journal_count_as_conference(self):
        self.assertEqual(_venue_kind({"journal": "Journal of Physics: Conference Series",
                                      "venue_type": "journal"}), "conference")
        self.assertEqual(_venue_kind({"journal": "arXiv", "venue_type": "repository"}), None)
        self.assertEqual(_venue_kind({"journal": "Nano Letters"}), "journal")
        for name in ("IFAC-PapersOnLine", "Lecture notes in computer science", "Procedia Computer Science",
                     "Studies in health technology and informatics", "2022 International Conference Laser Optics (ICLO)"):
            with self.subTest(name=name):
                self.assertEqual(_venue_kind({"journal": name}), "conference")
        # A repository is no venue kind at all, whatever its name looks like.
        self.assertIsNone(_venue_kind({"journal": "Zenodo (CERN European Organization for Nuclear Research)"}))
        self.assertIsNone(_venue_kind({"journal": "SSRN Electronic Journal"}))


class IsPreprintTest(unittest.TestCase):
    def test_registrant_prefix_must_match_whole(self):
        for doi in ("10.11016/x", "10.21390/x"):
            with self.subTest(doi=doi):
                self.assertFalse(_is_preprint({"doi": doi}))

    def test_preprint_doi_matches_with_or_without_resolver(self):
        for doi in ("10.1101/2020.01.01.123", "https://doi.org/10.1101/2020.01.01.123"):
            with self.subTest(doi=doi):
                self.assertTrue(_is_preprint({"doi": doi}))


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


    def test_report_is_ordered_by_record_pair(self):
        rows = []
        for i in range(10):
            title = f"Unique study {chr(97 + i) * 3} of nonlinear observers and estimators"
            rows += [pub(f"W{i}a", title, doi=f"10.1/{i}a", authors=[f"A{i}"]),
                     pub(f"W{i}b", title, doi=f"10.1/{i}b", authors=[f"A{i}"])]
        report = find_version_candidates(self._StubClient(rows))
        keys = [(row["record_a"], row["record_b"]) for row in report]
        self.assertEqual(len(keys), 10)
        self.assertEqual(keys, sorted(keys))

if __name__ == "__main__":
    unittest.main()
