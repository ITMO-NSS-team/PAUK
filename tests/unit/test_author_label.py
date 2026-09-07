import unittest

from pauk.gui.generate_data import author_label


class AuthorLabelTest(unittest.TestCase):
    def test_full_triplet_becomes_surname_and_initials(self):
        self.assertEqual(author_label("Иванов", "Иван", "Иванович"), "Иванов И.И.")

    def test_patronymic_only_is_used_as_the_initial(self):
        self.assertEqual(author_label("Иванов", None, "Иванович"), "Иванов И.")

    def test_private_build_writes_out_a_lone_given_name(self):
        # Without a patronymic, the given name stays written out in full on
        # the private build — --public must be the only thing that truncates it.
        self.assertEqual(author_label("Иванов", "Пётр", None), "Иванов Пётр")

    def test_a_bare_initial_given_name_gets_its_period_even_in_private(self):
        # This is data saying "only the initial is known", not our own
        # truncation for public — the period is correct punctuation either way.
        self.assertEqual(author_label("Иванов", "И", None), "Иванов И.")

    def test_a_given_name_already_carrying_a_period_is_not_doubled(self):
        self.assertEqual(author_label("Иванов", "И.", None), "Иванов И.")

    def test_surname_only(self):
        self.assertEqual(author_label("Иванов", None, None), "Иванов")

    def test_no_surname_returns_empty_the_caller_owns_the_fallback(self):
        # author_label no longer guesses at a combined raw/full-name string —
        # build_graph_data falls back to name_ru / the other language's label.
        self.assertEqual(author_label(None, "Иван", "Иванович"), "")
        self.assertEqual(author_label("", "", ""), "")

    def test_public_build_truncates_the_surname(self):
        self.assertEqual(author_label("Иванов", "Иван", "Иванович", public=True), "Ива.. И.И.")

    def test_public_build_leaves_short_surnames_alone(self):
        # 3 letters or fewer: truncating would save nothing, so don't.
        self.assertEqual(author_label("Ив", "Ан", None, public=True), "Ив А.")
        self.assertEqual(author_label("Ив", None, None, public=True), "Ив")

    def test_public_build_initials_a_lone_given_name_too(self):
        # Without a patronymic, the given name is written out in full on the
        # private build ("Горизонтова Мария") — --public must not leak that.
        self.assertEqual(author_label("Иванов", "Иван", None, public=True), "Ива.. И.")


if __name__ == "__main__":
    unittest.main()
