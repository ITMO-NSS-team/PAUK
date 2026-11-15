"""Candidate recall and workload tests, independent of merge decisions."""

import random
import unittest
from itertools import combinations
from unittest.mock import patch

from pauk.models import Person
from pauk.pipeline.person_name_blocking import (
    _compatible_given,
    _SurnameBlock,
    candidate_person_pairs,
    conflicting_initials,
    name_tokens,
    parse_name,
    person_name_forms,
)


def person(identifier, name=None, **kwargs):
    return Person(id=identifier, name_raw=name, is_itmo=True, **kwargs)


def pairs(people, scope=None, staff=None):
    return {tuple(sorted((a.id, b.id))) for a, b in candidate_person_pairs(people, scope, staff)}


class PersonNameBlockingTest(unittest.TestCase):
    def test_missing_names_do_not_block_exact_identifiers(self):
        for first_name, second_name in ((None, None), (None, "John Smith"), ("", "")):
            with self.subTest(names=(first_name, second_name)):
                people = [person("a", first_name, orcid="0000-0002-1825-0097"), person("b", second_name, orcid="0000-0002-1825-0097")]
                self.assertEqual(pairs(people), {("a", "b")})
                for entry in people:
                    entry.orcid = None
                self.assertEqual(pairs(people, staff={"a": "123", "b": "123"}), {("a", "b")})

    def test_orcid_url_and_bare_identifier_share_a_channel(self):
        self.assertEqual(
            pairs([person("a", orcid="https://orcid.org/0000-0002-1825-0097/"), person("b", orcid="0000-0002-1825-0097")]), {("a", "b")}
        )

    def test_missing_names_without_evidence_do_not_make_a_pair(self):
        self.assertEqual(pairs([person("a"), person("b")]), set())

    def test_parsed_fields_and_variants_work_without_display_names(self):
        for extra in ({"surname_en": "Petrov", "first_name_en": "Ivan"}, {"other_names": ["Ivan Petrov"]}):
            with self.subTest(extra=extra):
                self.assertEqual(pairs([person("a", **extra), person("b", "I. Petrov")]), {("a", "b")})

    def test_adding_the_65th_person_does_not_drop_existing_pairs(self):
        people = [person(str(i), "Ivan Petrov") for i in range(65)]
        before = pairs(people[:64])
        after = pairs(people)
        self.assertEqual(len(before), 2016)
        self.assertEqual(len(after), 2080)
        self.assertLessEqual(before, after)

    def test_large_parsed_surname_is_split_by_given_names(self):
        names = ("Ivan", "Boris", "Sergey", "Dmitry", "Nikolay", "Petr", "Oleg", "Roman", "Viktor", "Yakov")
        people = [person(f"{name}{i}", f"{name} Petrov", surname_en="Petrov") for name in names for i in range(20)]
        result = pairs(people)
        self.assertLess(len(result), 19900 // 2)
        for name in names:
            self.assertLessEqual(set(combinations(sorted(f"{name}{i}" for i in range(20)), 2)), result)

    def test_common_given_name_never_pairs_unrelated_surnames(self):
        people = [
            person("a", "Sergey Borisov"),
            person("b", "Petrov Sergey"),
            person("c", "Sergey Ivanov"),
            person("d", "Volchek Sergey"),
        ]
        self.assertEqual(pairs(people), set())

    def test_initials_and_different_given_names_do_not_pair_different_families(self):
        self.assertEqual(pairs([person("a", "A.A. Ivanov"), person("b", "A.A. Petrov")]), set())

    def test_names_with_changed_initials_remain_candidates(self):
        for first, second in (
            ("Ilya Makarov", "Lya Makarov"),
            ("Georgiy D. Kurganov", "Egor Kurganov"),
            ("Evgeny Petrov", "Yevgeny Petrov"),
            ("Dmitry Petrov", "Mitry Petrov"),
        ):
            with self.subTest(names=(first, second)):
                self.assertEqual(pairs([person("a", first), person("b", second)]), {("a", "b")})

    def test_diacritics_apostrophes_hyphens_and_particles(self):
        for first, second in (
            ("José García", "Jose Garcia"),
            ("Anna O’Connor", "Anna OConnor"),
            ("Ivan Petrov‐Sidorov", "Ivan Petrov-Sidorov"),
            ("Ivan Petrov-Sidorov", "I. Sidorov"),
            ("Ludwig van Beethoven", "van Beethoven, Ludwig"),
            ("Maria de la Cruz", "Cruz, Maria"),
            ("Petrov Ivan Ivanovich", "Ivan Petrov"),
            ("John Andrew Petrovich", "J. A. Petrovich"),
        ):
            with self.subTest(names=(first, second)):
                self.assertEqual(pairs([person("a", first), person("b", second)]), {("a", "b")})

    def test_compound_surnames_require_boundaries_not_arbitrary_prefixes(self):
        self.assertEqual(pairs([person("a", "Anna Ivanov"), person("b", "Anna Ivanovabramovich")]), set())
        self.assertEqual(pairs([person("a", "Anna Ivanov"), person("b", "Anna Ivanov-Abramovich")]), {("a", "b")})

    def test_comma_order_and_parsed_fields_determine_roles(self):
        for value in ("Malykh, Valentin", "Malykh Valentin"):
            with self.subTest(value=value):
                forms = parse_name(value, ("Malykh",))
                self.assertEqual({form.surname for form in forms}, {"malih"})
                self.assertEqual({form.given for form in forms}, {("valentin",)})

    def test_mixed_alphabets_and_initials(self):
        self.assertEqual(pairs([person("a", "Alexander А. Shtil"), person("b", "A. A. Shtil")]), {("a", "b")})
        self.assertEqual(name_tokens("A.A. Иванов"), ("a", "a", "ivanov"))

    def test_contaminated_variants_do_not_cross_unrelated_names(self):
        people = [person("a", "Leila Sharipova", name_variants=["Hao-Bin Fu"]), person("b", "Lianshe Fu")]
        self.assertEqual(pairs(people), set())

    def test_independent_coauthor_rescues_different_given_name(self):
        people = [
            person("a", "William Petrov", authored=[{"publication_id": "w1"}]),
            person("b", "Bill Petrov", authored=[{"publication_id": "w2"}]),
            person("c", "Jane Smith", authored=[{"publication_id": "w1"}, {"publication_id": "w2"}]),
        ]
        self.assertEqual(pairs(people), {("a", "b")})

    def test_context_does_not_pair_unrelated_surnames(self):
        people = [
            person("a", "Ivan Petrov", authored=[{"publication_id": "w"}]),
            person("b", "Ivan Sidorov", authored=[{"publication_id": "w"}]),
        ]
        self.assertEqual(pairs(people), set())

    def test_no_self_pairs_duplicates_or_input_order_effect(self):
        people = [
            person("a", "Ivan Petrov", surname_en="Petrov", name_variants=["Petrov, Ivan", "I. Petrov"]),
            person("b", "I. Petrov", orcid="0000-0002-1825-0097"),
            person("c", None, orcid="0000-0002-1825-0097"),
        ]
        expected = list((a.id, b.id) for a, b in candidate_person_pairs(people, None))
        self.assertEqual(len(expected), len(set(expected)))
        self.assertTrue(all(a != b for a, b in expected))
        for seed in range(10):
            random.Random(seed).shuffle(people)
            self.assertEqual(list((a.id, b.id) for a, b in candidate_person_pairs(people, None)), expected)

    def test_scope_keeps_cross_boundary_comparisons(self):
        people = [person(str(i), "Ivan Petrov") for i in range(3)]
        self.assertEqual(pairs(people, {"1"}), {("0", "1"), ("1", "2")})
        self.assertEqual(pairs(people, set()), set())

    def test_full_name_spacing_survives_common_given_name_statistics(self):
        people = [person("a", "Al-Abbass Mohamed"), person("b", "Al Abbass Mohamed")]
        people.extend(person(f"x{i}", f"Mohamed Surname{i}") for i in range(100))
        self.assertIn(("a", "b"), pairs(people))

    def test_explicit_space_separated_compound_surname(self):
        self.assertEqual(
            pairs([person("a", "Ivan Petrov Sidorov", surname_en="Petrov Sidorov"), person("b", "I. Petrov")]),
            {("a", "b")},
        )

    def test_surname_with_particles_and_no_given_name(self):
        self.assertEqual(pairs([person("a", "van Beethoven"), person("b", "Ludwig van Beethoven")]), {("a", "b")})

    def test_context_index_matches_explicit_coauthors(self):
        names = ("William", "Bill", "Alice", "Zoe", "Robert", "Boris")
        for seed in range(80):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                works = {str(i): {f"w{j}" for j in range(5) if rng.random() < 0.35} for i in range(len(names))}
                people = [
                    person(
                        str(i),
                        f"{name} Petrov",
                        surname_en="Petrov",
                        authored=[{"publication_id": w} for w in sorted(works[str(i)])],
                    )
                    for i, name in enumerate(names)
                ]
                coauthors = {a: {b for b in works if a != b and works[a] & works[b]} for a in works}
                expected = set()
                for i, j in combinations(range(len(names)), 2):
                    a, b = str(i), str(j)
                    if (
                        _compatible_given(name_tokens(names[i]), name_tokens(names[j]))
                        or works[a] & works[b]
                        or coauthors[a] & coauthors[b]
                    ):
                        expected.add((a, b))
                self.assertEqual(pairs(people), expected)

    def test_particles_can_also_be_complete_asian_surnames(self):
        for surname in ("Le", "Lê", "La", "De", "Van"):
            with self.subTest(surname=surname):
                self.assertEqual(pairs([person("a", f"Anh {surname}"), person("b", f"A. {surname}")]), {("a", "b")})
                self.assertEqual(pairs([person("a", f"Anh {surname}"), person("b", f"{surname} Anh")]), {("a", "b")})

    def test_transliteration_alias_keeps_original_initial(self):
        self.assertEqual(pairs([person("a", "Yevgeny Petrov"), person("b", "Y. Petrov")]), {("a", "b")})
        self.assertEqual(pairs([person("a", "Yevgeny Petrov"), person("b", "E. Petrov")]), {("a", "b")})

    def test_large_collaboration_does_not_expand_all_coauthor_edges(self):
        people = [
            person(
                str(i),
                f"Ivan Surname{i}",
                surname_en=f"Surname{i}",
                authored=[{"publication_id": "w1"}, {"publication_id": "w2"}],
            )
            for i in range(200)
        ]
        original = _SurnameBlock.add
        sizes = []

        def record(block, identifier, given, channels):
            sizes.append(sum(key.startswith("context:") for key in channels))
            return original(block, identifier, given, channels)

        with patch.object(_SurnameBlock, "add", record):
            pairs(people)
        # A consortium cannot rescue incompatible personal names, and no
        # quadratic coauthor expansion is materialized for its participants.
        self.assertTrue(sizes)
        self.assertEqual(max(sizes), 0)

    def test_titles_and_numeric_suffixes_are_not_name_roles(self):
        self.assertEqual(name_tokens("Dr. Ahmad Azam Malik (6166283)"), ("ahmad", "azam", "malik"))
        self.assertEqual(name_tokens("Prof. Ivan Petrov"), ("ivan", "petrov"))

    def test_record_specific_surname_evidence_beats_global_ambiguity(self):
        people = [person("a", "Ahmad Azam Malik", name_variants=["Malik, Ahmad Azam"])]
        people.extend(person(str(i), f"Name{i} Ahmad") for i in range(10))
        self.assertEqual({form.surname for form in person_name_forms(people)["a"]}, {"malik"})

    def test_ambiguous_reversed_name_cannot_be_rescued_by_collaborators(self):
        people = [
            person("a", "Ali H Mokdad", authored=[{"publication_id": "w1"}]),
            person("b", "Irfan Ali", authored=[{"publication_id": "w2"}]),
            person("c", "Jane Smith", authored=[{"publication_id": "w1"}, {"publication_id": "w2"}]),
        ]
        self.assertNotIn(("a", "b"), pairs(people))

    def test_mass_collaboration_does_not_rescue_different_names(self):
        people = [
            person("a", "William Petrov", authored=[{"publication_id": "w1"}]),
            person("b", "Bill Petrov", authored=[{"publication_id": "w2"}]),
        ]
        people.extend(
            person(f"x{i}", f"Other Surname{i}", authored=[{"publication_id": "w1"}, {"publication_id": "w2"}])
            for i in range(60)
        )
        self.assertNotIn(("a", "b"), pairs(people))
        people[1].name_raw = "W. Petrov"
        self.assertIn(("a", "b"), pairs(people))
        people[1].name_raw = "Bill Petrov"
        self.assertIn(("a", "b"), pairs(people, staff={"a": "staff", "b": "staff"}))

    def test_fuzzy_surnames_do_not_hide_initial_conflicts(self):
        for first, second in (("М. В. Бочкарев", "M.D. Bochkareva"), ("A. S. Ivanov", "Ivanova, A. K.")):
            with self.subTest(names=(first, second)):
                self.assertTrue(conflicting_initials(person("a", first), person("b", second)))

    def test_missing_or_compatible_initials_do_not_conflict(self):
        for first, second in (
            ("Victoria A. Scherbakova", "V. A. Scherbackova"),
            ("Ivanov, A.", "A. S. Ivanov"),
            ("Yevgeny Petrov", "E. Petrov"),
            ("Petrov Ivan Ivanovich", "I. I. Petrov"),
            (None, "M. Smith"),
        ):
            with self.subTest(names=(first, second)):
                self.assertFalse(conflicting_initials(person("a", first), person("b", second)))

    def test_uncertain_name_roles_and_transliterated_initials_are_not_vetoes(self):
        for first, second in (
            ("Nguyen Khac Tung", "K.T. Nguyen"),
            ("Evgeny A. Viktorov", "Viktorov A. Evgeny"),
            ("Е. Yu. Vasilyeva", "E. U. Vasileva"),
            ("Yulia Salii", "Julia A Guseva"),
        ):
            with self.subTest(names=(first, second)):
                self.assertFalse(conflicting_initials(person("a", first), person("b", second)))


if __name__ == "__main__":
    unittest.main()
