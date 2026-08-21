import unittest

from pauk.gui.generate_data import author_label, split_full_name


def label(*, surname=None, given=None, patronymic=None, name_en=None, name_ru=None):
    return author_label(surname, given, patronymic, name_en, name_ru)


class AuthorLabelTest(unittest.TestCase):
    def test_catalog_record_becomes_surname_and_initials(self):
        self.assertEqual(
            label(surname="Иванов", given="Иван", patronymic="Иванович"),
            "Иванов И.И.")

    def test_no_separate_name_parts_only_the_full_ru_name(self):
        self.assertEqual(label(name_ru="Иван Иванович Иванов", name_en="Ivan Ivanovich Ivanov"),
                         "Иванов И.И.")
        self.assertEqual(label(name_ru="Иван А. Иванов"), "Иванов И.А.")
        self.assertEqual(label(name_ru="А. А. Иванов"), "Иванов А.А.")

    def test_without_a_patronymic_the_given_name_stays_written_out(self):
        self.assertEqual(label(name_ru="Иван Иванов"), "Иванов Иван")
        self.assertEqual(label(surname="Иванов", given="Пётр"), "Иванов Пётр")

    def test_falls_back_to_the_romanized_name(self):
        self.assertEqual(label(name_en="Ivan Ivanov"), "Ivanov Ivan")
        self.assertEqual(label(), "")

    def test_single_word_name_is_left_alone(self):
        self.assertEqual(label(name_ru="Иванов"), "Иванов")

    def test_public_build_truncates_the_surname(self):
        self.assertEqual(
            author_label("Иванов", "Иван", "Иванович", None, public=True),
            "Ива.. И.И.")

    def test_public_build_leaves_short_surnames_alone(self):
        # 3 letters or fewer: truncating would save nothing, so don't.
        self.assertEqual(author_label("Ив", "Ан", None, None, public=True), "Ив А.")
        self.assertEqual(author_label("Ив", None, None, None, public=True), "Ив")

    def test_public_build_initials_a_lone_given_name_too(self):
        # Without a patronymic, the given name is written out in full on the
        # private build ("Горизонтова Мария") — --public must not leak that.
        self.assertEqual(author_label("Иванов", "Иван", None, None, public=True), "Ива.. И.")


class SplitFullNameTest(unittest.TestCase):
    def test_given_name_first_is_the_common_case(self):
        self.assertEqual(split_full_name("Иван Иванович Иванов"),
                         ("Иванов", "Иван", "Иванович"))

    def test_surname_first_with_a_comma(self):
        self.assertEqual(split_full_name("Иванов, Иван А."),
                         ("Иванов", "Иван", "А."))

    def test_lowercase_particles_never_become_initials(self):
        self.assertEqual(split_full_name("Ян ван дер Берг"), ("Берг", "Ян", ""))

    def test_blank_input(self):
        self.assertEqual(split_full_name(None), ("", "", ""))


if __name__ == "__main__":
    unittest.main()
