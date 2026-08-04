import unittest

from pauk.gui.generate_data import author_label, split_full_name


def label(*, surname=None, given=None, patronymic=None, name_en=None, name_ru=None):
    return author_label(surname, given, patronymic, name_en, name_ru)


class AuthorLabelTest(unittest.TestCase):
    def test_catalog_record_becomes_surname_and_initials(self):
        self.assertEqual(
            label(surname="Никитин", given="Николай", patronymic="Олегович"),
            "Никитин Н.О.")

    def test_transliterated_name_is_reordered_the_same_way(self):
        # No separate name parts, only the full Russian name — the label
        # must still read surname-first.
        self.assertEqual(label(name_ru="Виктория Вадимовна Юношева", name_en="Victoria Vadimovna Yunosheva"),
                         "Юношева В.В.")
        self.assertEqual(label(name_ru="Валерия А. Пьянченкова"), "Пьянченкова В.А.")
        self.assertEqual(label(name_ru="А. А. Мусаев"), "Мусаев А.А.")

    def test_a_cyrillic_full_name_reads_surname_first(self):
        # The source wrote "Фамилия Имя Отчество"; reading the surname off
        # the end would sign the card "Дмитриевич К.М.".
        self.assertEqual(label(name_en="Кучин Михаил Дмитриевич"), "Кучин М.Д.")
        self.assertEqual(label(name_ru="Муслимов Тагир Забирович"), "Муслимов Т.З.")

    def test_a_surname_ending_like_a_patronymic_is_not_one(self):
        # Олехнович and Масалович are surnames. The first arrives with an
        # initial, the second behind a patronymic of its own — neither is
        # the third of three spelled-out words.
        self.assertEqual(label(name_ru="Роман О. Олехнович"), "Олехнович Р.О.")
        self.assertEqual(label(name_ru="Мария Ивановна Масалович"), "Масалович М.И.")

    def test_without_a_patronymic_the_given_name_stays_written_out(self):
        self.assertEqual(label(name_ru="Мария Горизонтова"), "Горизонтова Мария")
        self.assertEqual(label(surname="Борисова", given="Юлия"), "Борисова Юлия")

    def test_falls_back_to_the_romanized_name(self):
        self.assertEqual(label(name_en="Wei Wang"), "Wang Wei")
        self.assertEqual(label(), "")

    def test_single_word_name_is_left_alone(self):
        self.assertEqual(label(name_ru="Осьмак"), "Осьмак")


class SplitFullNameTest(unittest.TestCase):
    def test_given_name_first_is_the_common_case(self):
        self.assertEqual(split_full_name("Николай Олегович Никитин"),
                         ("Никитин", "Николай", "Олегович"))

    def test_surname_first_with_a_comma(self):
        self.assertEqual(split_full_name("Шипиловских, Сергей А."),
                         ("Шипиловских", "Сергей", "А."))

    def test_lowercase_particles_never_become_initials(self):
        self.assertEqual(split_full_name("Ян ван дер Берг"), ("Берг", "Ян", ""))

    def test_blank_input(self):
        self.assertEqual(split_full_name(None), ("", "", ""))


if __name__ == "__main__":
    unittest.main()
