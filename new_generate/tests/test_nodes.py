"""Юнит-тесты для `nodes.py`.

`author_label`-тесты перенесены из `tests/unit/test_author_label.py` (тот
файл проверяет `pauk.gui.generate_data`, не `new_generate` — здесь та же
проверка для переписанной версии). DenseRankTest перенесён из бывшего
`test_ranking.py` вместе с самой функцией (единственный реальный
потребитель — этот модуль, отдельный `ranking.py` был лишним)."""

from __future__ import annotations

import unittest

from new_generate.nodes import author_label, author_variants, dense_rank


class DenseRankTest(unittest.TestCase):
    def test_ties_get_equal_top_rank(self):
        self.assertEqual(dense_rank({"a": 1, "b": 5, "c": 5}), {"a": 0.5, "b": 1.0, "c": 1.0})

    def test_all_equal_values_all_rank_one(self):
        self.assertEqual(dense_rank({"a": 3, "b": 3}), {"a": 1.0, "b": 1.0})

    def test_single_value(self):
        self.assertEqual(dense_rank({"a": 10}), {"a": 1.0})


class AuthorLabelTest(unittest.TestCase):
    def test_full_triplet_becomes_surname_and_initials(self):
        self.assertEqual(author_label("Иванов", "Иван", "Иванович"), "Иванов И.И.")

    def test_patronymic_only_is_used_as_the_initial(self):
        self.assertEqual(author_label("Иванов", None, "Иванович"), "Иванов И.")

    def test_private_build_writes_out_a_lone_given_name(self):
        # Без отчества имя пишется полностью в приватной сборке -
        # усекать его должен только --public.
        self.assertEqual(author_label("Иванов", "Пётр", None), "Иванов Пётр")

    def test_a_bare_initial_given_name_gets_its_period_even_in_private(self):
        # Это данные, говорящие "известен только инициал", а не наше
        # собственное усечение для public - точка верна в любом случае.
        self.assertEqual(author_label("Иванов", "И", None), "Иванов И.")

    def test_a_given_name_already_carrying_a_period_is_not_doubled(self):
        self.assertEqual(author_label("Иванов", "И.", None), "Иванов И.")

    def test_surname_only(self):
        self.assertEqual(author_label("Иванов", None, None), "Иванов")

    def test_no_surname_returns_empty_the_caller_owns_the_fallback(self):
        # author_label не угадывает сборную сырую строку -
        # AuthorNodeBuilder откатывается на name_ru / подпись другого языка.
        self.assertEqual(author_label(None, "Иван", "Иванович"), "")
        self.assertEqual(author_label("", "", ""), "")

    def test_public_build_truncates_the_surname(self):
        self.assertEqual(author_label("Иванов", "Иван", "Иванович", public=True), "Ива.. И.И.")

    def test_public_build_leaves_short_surnames_alone(self):
        # 3 буквы и меньше: усекать нечего, экономии не будет.
        self.assertEqual(author_label("Ив", "Ан", None, public=True), "Ив А.")
        self.assertEqual(author_label("Ив", None, None, public=True), "Ив")

    def test_public_build_initials_a_lone_given_name_too(self):
        # Без отчества имя пишется полностью в приватной сборке
        # ("Горизонтова Мария") - --public не должен это утечь.
        self.assertEqual(author_label("Иванов", "Иван", None, public=True), "Ива.. И.")


class AuthorVariantsTest(unittest.TestCase):
    def test_excludes_already_shown_labels(self):
        row = {"name_ru": "Иванов И.И.", "name_variants": ["Ivanov I.I.", "И. Иванов"]}
        variants = author_variants(row, label_ru="Иванов И.И.", label_en="Ivanov I.I.")
        self.assertEqual(variants, {"openalex": ["И. Иванов"], "orcid": []})

    def test_excludes_full_name_ru_and_en_too(self):
        """name_ru/name_en теперь сами становятся заголовком карточки (см.
        docstring author_variants) - не должны повторно всплывать в списке."""
        row = {
            "name_ru": "Иванов Иван Иванович",
            "name_en": "Ivan Ivanov",
            "name_variants": ["Ivan Ivanov", "И. Иванов"],
        }
        variants = author_variants(row, label_ru="Иванов И.И.", label_en="Ivanov I.I.")
        self.assertEqual(variants, {"openalex": ["И. Иванов"], "orcid": []})

    def test_deduplicates_case_insensitively_within_one_source(self):
        row = {"name_ru": "", "name_variants": ["A B", "a b", "C D"]}
        variants = author_variants(row, label_ru="x", label_en="y")
        self.assertEqual(variants, {"openalex": ["A B", "C D"], "orcid": []})

    def test_other_names_go_to_orcid_group_separately_from_name_variants(self):
        row = {"name_ru": "", "name_variants": ["Ivan Ivanov"], "other_names": ["I. Ivanov", "Ivan I."]}
        variants = author_variants(row, label_ru="x", label_en="y")
        self.assertEqual(variants, {"openalex": ["Ivan Ivanov"], "orcid": ["I. Ivanov", "Ivan I."]})

    def test_no_variants_returns_empty_groups(self):
        row = {"name_ru": "", "name_variants": [], "other_names": []}
        self.assertEqual(author_variants(row, "x", "y"), {"openalex": [], "orcid": []})
