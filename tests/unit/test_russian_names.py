import tempfile
import unittest
from pathlib import Path

import json

from pauk.models import Person
from pauk.pipeline.stages.russian_names import (
    AMBIGUOUS_FILENAME,
    RussianNamesStage,
    to_cyrillic,
)
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore

CATALOG_HEADER = "name_ru,surname,name,patronymic,degree\n"


def person(pid, name, *, variants=(), degree=None):
    return Person(id=pid, openalex_id=pid, is_itmo=True, name_en=name,
                  name_variants=list(variants), degree=degree)


class RussianNamesStageTest(unittest.TestCase):
    def run_stage(self, people, catalog_rows):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = Settings(data_dir=root)
        config.static_dir.mkdir(parents=True)
        (config.static_dir / "russian_names.csv").write_text(
            CATALOG_HEADER + "".join(f"{row}\n" for row in catalog_rows), encoding="utf-8")
        self.prepared = PreparedStore(config.prepared_dir, "sample")
        self.raw = RawStore(config.raw_dir, "sample")
        self.prepared.write_models("persons", people)
        result = RussianNamesStage(self.prepared, self.raw, config).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def ambiguous(self):
        path = self.prepared.group_dir / AMBIGUOUS_FILENAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_missing_catalog_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Settings(data_dir=root)
            prepared = PreparedStore(config.prepared_dir, "sample")
            prepared.write_models("persons", [person("A1", "Nikolay Nikitin")])
            with self.assertRaises(FileNotFoundError):
                RussianNamesStage(prepared, RawStore(config.raw_dir, "sample"), config).run()

    def test_catalog_match_fills_official_record(self):
        result, people = self.run_stage(
            [person("A1", "Nikolay O. Nikitin", degree=None)],
            ["Никитин Николай Олегович,Никитин,Николай,Олегович,к.т.н."],
        )
        self.assertEqual(result["names_from_catalog"], 1)
        row = people["A1"]
        self.assertEqual(row.name_ru, "Никитин Николай Олегович")
        self.assertEqual((row.first_name_ru, row.second_name_ru, row.surname_ru),
                         ("Николай", "Олегович", "Никитин"))
        self.assertEqual(row.degree, "к.т.н.")
        self.assertEqual(row.processing["russian_names"].status, "completed")

    def test_romanization_variants_still_match(self):
        for spelling in ("Aleksei Dukhanov", "Alexey Dukhanov", "Aleksey V. Dukhanov"):
            _, people = self.run_stage(
                [person("A1", spelling)],
                ["Духанов Алексей Валентинович,Духанов,Алексей,Валентинович,к.т.н."],
            )
            self.assertEqual(people["A1"].surname_ru, "Духанов", spelling)

    def test_match_via_name_variant(self):
        _, people = self.run_stage(
            [person("A1", "J. Borisova", variants=["Julia Borisova"])],
            ["Борисова Юлия Андреевна,Борисова,Юлия,Андреевна,"],
        )
        self.assertEqual(people["A1"].name_ru, "Борисова Юлия Андреевна")

    def test_namesake_catalog_rows_never_match(self):
        # Two official records behind one name: matching would hand this
        # person someone else's record, so the key is dropped entirely.
        result, people = self.run_stage(
            [person("A1", "Ivan Smirnov")],
            [
                "Смирнов Иван Петрович,Смирнов,Иван,Петрович,к.т.н.",
                "Смирнов Иван Васильевич,Смирнов,Иван,Васильевич,д.х.н.",
            ],
        )
        self.assertEqual(result["names_from_catalog"], 0)
        self.assertEqual(people["A1"].name_ru, "Иван Смирнов")  # transliterated

    def test_namesakes_are_journalled_with_their_candidates(self):
        result, _ = self.run_stage(
            [person("A1", "Ivan Smirnov"), person("A2", "Pavel Zhukov")],
            [
                "Смирнов Иван Петрович,Смирнов,Иван,Петрович,к.т.н.",
                "Смирнов Иван Васильевич,Смирнов,Иван,Васильевич,д.х.н.",
            ],
        )
        self.assertEqual(result["names_ambiguous"], 1)
        (row,) = self.ambiguous()
        self.assertEqual((row["person"], row["name_en"]), ("A1", "Ivan Smirnov"))
        self.assertEqual([c["name_ru"] for c in row["candidates"]],
                         ["Смирнов Иван Петрович", "Смирнов Иван Васильевич"])
        self.assertEqual([c["degree"] for c in row["candidates"]], ["к.т.н.", "д.х.н."])

    def test_initial_only_collision_with_a_different_given_name_is_not_reported(self):
        # "A. Polyakov" keys collide with every namesake, but a person
        # written out as Andrey matches none of them — they are simply
        # absent from the catalog, not an ambiguity anyone can resolve.
        result, _ = self.run_stage(
            [person("A1", "Andrey Polyakov")],
            [
                "Поляков Антон Александрович,Поляков,Антон,Александрович,",
                "Поляков Александр Сергеевич,Поляков,Александр,Сергеевич,",
            ],
        )
        self.assertEqual(result["names_ambiguous"], 0)
        self.assertEqual(self.ambiguous(), [])

    def test_initials_only_person_is_still_reported(self):
        result, _ = self.run_stage(
            [person("A1", "A. Polyakov")],
            [
                "Поляков Антон Александрович,Поляков,Антон,Александрович,",
                "Поляков Александр Сергеевич,Поляков,Александр,Сергеевич,",
            ],
        )
        self.assertEqual(result["names_ambiguous"], 1)

    def test_an_initials_variant_does_not_resurrect_a_ruled_out_person(self):
        # The collision happens on the "A. Polyakov" variant, but the
        # person is written out as Andrey elsewhere — still not a case
        # anyone can resolve.
        result, _ = self.run_stage(
            [person("A1", "Andrey Polyakov", variants=["A. Polyakov"])],
            [
                "Поляков Антон Александрович,Поляков,Антон,Александрович,",
                "Поляков Александр Сергеевич,Поляков,Александр,Сергеевич,",
            ],
        )
        self.assertEqual(result["names_ambiguous"], 0)

    def test_journal_is_rewritten_each_run(self):
        # A name that stopped being ambiguous must not linger in the file.
        self.run_stage(
            [person("A1", "Ivan Smirnov")],
            ["Смирнов Иван Петрович,Смирнов,Иван,Петрович,к.т.н."],
        )
        self.assertEqual(self.ambiguous(), [])

    def test_transliteration_fallback(self):
        result, people = self.run_stage([person("A1", "Pavel V. Zhukov")], [])
        self.assertEqual(result["names_transliterated"], 1)
        self.assertEqual(people["A1"].name_ru, "Павел В. Жуков")
        self.assertIsNone(people["A1"].surname_ru)  # parts are never guessed

    def test_existing_degree_is_not_overwritten(self):
        _, people = self.run_stage(
            [person("A1", "Nikolay O. Nikitin", degree="PhD")],
            ["Никитин Николай Олегович,Никитин,Николай,Олегович,к.т.н."],
        )
        self.assertEqual(people["A1"].degree, "PhD")

    def test_second_run_is_a_no_op(self):
        self.run_stage([person("A1", "Pavel Zhukov")], [])
        again = RussianNamesStage(self.prepared, self.raw,
                                  Settings(data_dir=Path(self.tmp.name))).run()
        self.assertEqual(again["russian_names"], 0)


class ToCyrillicTest(unittest.TestCase):
    def test_common_romanizations(self):
        for latin, cyrillic in (
            ("Nikolay", "Николай"),
            ("Sergey", "Сергей"),
            ("Evgeny", "Евгений"),
            ("Yuri", "Юрий"),
            ("Julia", "Юлия"),
            ("Tatyana", "Татьяна"),
            ("Alexey", "Алексей"),
            ("Aleksei", "Алексей"),
            ("Mikhail", "Михаил"),
            ("Nikolai", "Николай"),
            ("Shcherbakov", "Щербаков"),
            ("Tsvetkova", "Цветкова"),
            ("Petrova-Sidorova", "Петрова-Сидорова"),
            # Non-Russian names go through the same rules; the result is
            # only ever a readable approximation.
            ("Wei Wang", "Вей Ванг"),
        ):
            self.assertEqual(to_cyrillic(latin), cyrillic, latin)

    def test_given_names_that_per_character_rules_get_wrong(self):
        # Names the letter rules cannot reach: a soft sign with no letter
        # of its own ("Ilya", "Olga"), ё written as e, or a Latinized form
        # ("Alexander", "Eugene").
        for latin, cyrillic in (
            ("Ilya", "Илья"),
            ("Olga", "Ольга"),
            ("Igor", "Игорь"),
            ("Daria", "Дарья"),
            ("Petr", "Пётр"),
            ("Fedor", "Фёдор"),
            ("Semen", "Семён"),
            ("Artem", "Артём"),
            ("Alexander", "Александр"),
            ("Eugene", "Евгений"),
            ("Vyacheslav", "Вячеслав"),
            ("Lyubov", "Любовь"),
            ("Nikita", "Никита"),
        ):
            self.assertEqual(to_cyrillic(latin), cyrillic, latin)

    def test_every_spelling_variant_reaches_one_entry(self):
        for spelling in ("Ilya", "Ilia", "Ilja", "Iliya"):
            self.assertEqual(to_cyrillic(spelling), "Илья", spelling)
        for spelling in ("Tatiana", "Tatyana"):
            self.assertEqual(to_cyrillic(spelling), "Татьяна", spelling)
        for spelling in ("Alexander", "Aleksandr", "Aleksander"):
            self.assertEqual(to_cyrillic(spelling), "Александр", spelling)

    def test_diphthongs_and_soft_signs_in_surnames(self):
        for latin, cyrillic in (
            ("Zaytsev", "Зайцев"),        # y closing a diphthong
            ("Zaitsev", "Зайцев"),        # the same surname spelled with i
            ("Voytenko", "Войтенко"),
            ("Nikolayev", "Николаев"),    # y between vowels is not a letter
            ("Sergeyev", "Сергеев"),
            ("Vasilyev", "Васильев"),     # y after a consonant before e
            ("Grigoryev", "Григорьев"),
            ("Ulyanov", "Ульянов"),
            ("Lukyanov", "Лукьянов"),
            ("Tretyakov", "Третьяков"),
            ("Kudryavtseva", "Кудрявцева"),  # ...but after r it is plain я
            ("Ryabov", "Рябов"),
            ("Myasnikov", "Мясников"),
            ("Kolyubin", "Колюбин"),      # ...and yu after a consonant is ю
            ("Yevgeny", "Евгений"),       # leading ye
            ("Mikhail", "Михаил"),        # "ai" here is two vowels, not ай
            ("Rudyi", "Рудый"),           # closing yi is the ый ending
            ("Bezrodnyi", "Безродный"),
            ("Ilyina", "Ильина"),         # ...inside a word it is a soft sign
        ):
            self.assertEqual(to_cyrillic(latin), cyrillic, latin)

    def test_initials_keep_their_case(self):
        self.assertEqual(to_cyrillic("S.S. Rudyi"), "С.С. Рудый")
        self.assertEqual(to_cyrillic("A. A. Musaev"), "А. А. Мусаев")
        # Glued initials are capitalized one by one, not once per word.
        self.assertEqual(to_cyrillic("I.Yu. Nikitin"), "И.Ю. Никитин")
        self.assertEqual(to_cyrillic("A.V.Petrov"), "А.В.Петров")

    def test_dictionary_applies_per_word_not_to_surnames(self):
        # "Lev" is a given name here and a surname stem elsewhere; the
        # dictionary is per word, so the surname keeps its own rules.
        self.assertEqual(to_cyrillic("Lev Utkin"), "Лев Уткин")
        self.assertEqual(to_cyrillic("Olga Ilina"), "Ольга Илина")


if __name__ == "__main__":
    unittest.main()
