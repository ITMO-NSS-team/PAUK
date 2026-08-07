import tempfile
import unittest
from pathlib import Path

import json

from pauk.models import Person
from pauk.pipeline.stages.russian_names import (
    AMBIGUOUS_FILENAME,
    RussianNamesCatalog,
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

    def test_a_withdrawn_record_takes_its_parts_with_it(self):
        # The rule tightened between runs: this spelling no longer names
        # anyone, and the official parts must not outlive the match that
        # wrote them — the card is signed from them, not from name_ru.
        catalog = ["Дмитриев Алексей Андреевич,Дмитриев,Алексей,Андреевич,к.т.н."]
        _, people = self.run_stage([person("A1", "Alexey A. Dmitriev")], catalog)
        self.assertEqual(people["A1"].surname_ru, "Дмитриев")

        named = people["A1"]
        named.name_en = "A. D. Dmitriev"
        named.processing = {}
        _, people = self.run_stage([named], catalog)
        row = people["A1"]
        self.assertEqual(row.name_ru, "А. Д. Дмитриев")
        self.assertEqual((row.first_name_ru, row.second_name_ru, row.surname_ru, row.degree),
                         (None, None, None, None))

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

    def test_a_cyrillic_letter_inside_a_latin_name_still_matches(self):
        # The В, С and Х here are Cyrillic; they fold to v, s and h where
        # their Latin lookalikes fold to b, c and ks.
        for spelling in ("V.A. Вogatyrev", "Vladimir А. Вogatyrev", "V. А. Bogatyrev"):
            _, people = self.run_stage(
                [person("A1", spelling)],
                ["Богатырев Владимир Анатольевич,Богатырев,Владимир,Анатольевич,д.т.н."],
            )
            self.assertEqual(people["A1"].surname_ru, "Богатырев", spelling)

    def test_a_name_written_surname_first_with_a_comma_matches(self):
        # OpenAlex serves "Ivanov, Ilya" beside "Ilya Ivanov"; a comma left
        # in the folded key keeps the two spellings apart.
        for spelling in ("Ivanov, Ilya", "Иванов, Илья Петрович"):
            _, people = self.run_stage(
                [person("A1", spelling)],
                ["Иванов Илья Петрович,Иванов,Илья,Петрович,"],
            )
            self.assertEqual(people["A1"].name_ru, "Иванов Илья Петрович", spelling)

    def test_one_employee_listed_twice_is_still_one_record(self):
        # Two rows for one person read as namesakes: their shared key is
        # dropped and the employee stops matching at all.
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov")],
            ["Петров Иван Сергеевич,Петров,Иван,Сергеевич,",
             "Петров Иван Сергеевич,Петров,Иван,Сергеевич,к.т.н."],
        )
        self.assertEqual(result["names_from_catalog"], 1)
        self.assertEqual(people["A1"].name_ru, "Петров Иван Сергеевич")
        # The degree stated by one of the rows survives the merge.
        self.assertEqual(people["A1"].degree, "к.т.н.")
        self.assertEqual(self.ambiguous(), [])

    def test_a_run_that_examines_nobody_keeps_the_journal(self):
        self.run_stage(
            [person("A1", "Ivan Smirnov")],
            ["Смирнов Иван Петрович,Смирнов,Иван,Петрович,",
             "Смирнов Иван Сергеевич,Смирнов,Иван,Сергеевич,"],
        )
        self.assertEqual(len(self.ambiguous()), 1)
        again = RussianNamesStage(self.prepared, self.raw,
                                  Settings(data_dir=Path(self.tmp.name))).run()
        self.assertEqual(again["russian_names"], 0)
        self.assertEqual(len(self.ambiguous()), 1)

    def test_a_cyrillic_full_name_fills_the_parts_without_the_catalog(self):
        for spelling in ("Илья Алексеевич Суров", "Суров Илья Алексеевич",
                         "Суров, Илья Алексеевич"):
            result, people = self.run_stage([person("A1", spelling)], [])
            self.assertEqual(result["names_from_own_spelling"], 1, spelling)
            row = people["A1"]
            self.assertEqual((row.surname_ru, row.first_name_ru, row.second_name_ru),
                             ("Суров", "Илья", "Алексеевич"), spelling)
            self.assertEqual(row.name_ru, "Суров Илья Алексеевич", spelling)

    def test_a_full_name_is_read_from_a_variant_too(self):
        _, people = self.run_stage(
            [person("A1", "A. V. Malyshev", variants=["Алексей Владимирович Малышев"])], [])
        self.assertEqual(people["A1"].second_name_ru, "Владимирович")

    def test_a_surname_ending_like_a_patronymic_invents_nothing(self):
        # Томкович is a surname; reading "М. В. Томкович" as a full name
        # would hand the person a patronymic nobody stated.
        for spelling in ("М. В. Томкович", "Е.И. Олехнович", "Ольга Бабич"):
            result, people = self.run_stage([person("A1", spelling)], [])
            self.assertEqual(result["names_from_own_spelling"], 0, spelling)
            self.assertIsNone(people["A1"].second_name_ru, spelling)

    def test_the_catalog_wins_over_the_authors_own_spelling(self):
        _, people = self.run_stage(
            [person("A1", "Суров Илья Алексеевич")],
            ["Суров Илья Алексеевич,Суров,Илья,Алексеевич,к.ф.-м.н."],
        )
        self.assertEqual(people["A1"].degree, "к.ф.-м.н.")

    def test_english_spellings_of_a_given_name_match(self):
        for spelling, catalog_row in (
            ("Alexander Ivanov", "Иванов Александр Петрович,Иванов,Александр,Петрович,"),
            ("Victoria Ivanova", "Иванова Виктория Петровна,Иванова,Виктория,Петровна,"),
            ("Peter Ivanov", "Иванов Пётр Петрович,Иванов,Пётр,Петрович,"),
        ):
            _, people = self.run_stage([person("A1", spelling)], [catalog_row])
            self.assertEqual(people["A1"].name_ru, catalog_row.split(",")[0], spelling)

    def test_ch_is_not_read_as_a_latin_c(self):
        # The rule that turns "c" into к must leave the ч and щ digraphs alone.
        _, people = self.run_stage(
            [person("A1", "Ivan Chernyshov")],
            ["Чернышов Иван Петрович,Чернышов,Иван,Петрович,"],
        )
        self.assertEqual(people["A1"].surname_ru, "Чернышов")

    def test_an_initial_that_folds_to_two_characters_still_matches(self):
        # "Ю" folds to "iu", so a record keyed by the first character of the
        # folded patronymic alone is unreachable from a "Yu." citation.
        for spelling in ("Olga Yu. Orlova", "O.Yu. Orlova", "O. Y. Orlova"):
            _, people = self.run_stage(
                [person("A1", spelling)],
                ["Орлова Ольга Юрьевна,Орлова,Ольга,Юрьевна,"],
            )
            self.assertEqual(people["A1"].name_ru, "Орлова Ольга Юрьевна", spelling)

    def test_a_cyrillic_word_between_latin_ones_is_left_alone(self):
        # Deciding the alphabet over the whole name would rewrite the
        # patronymic into a mixture and lose the record.
        _, people = self.run_stage(
            [person("A1", "Maria Алексеевна Yaroslavova")],
            ["Ярославова Мария Алексеевна,Ярославова,Мария,Алексеевна,"],
        )
        self.assertEqual(people["A1"].name_ru, "Ярославова Мария Алексеевна")


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


class StaffIdentityTest(unittest.TestCase):
    """What the catalog is willing to claim as an identity, which is a
    stricter question than what it is willing to write onto a card: dedup
    folds two person records together on this answer."""

    @staticmethod
    def catalog(*rows):
        return RussianNamesCatalog([
            dict(zip(("name_ru", "surname", "name", "patronymic", "degree"), row))
            for row in rows
        ])

    def dukhanov(self):
        return self.catalog(("Духанов Алексей Валентинович", "Духанов",
                             "Алексей", "Валентинович", "д.т.н."))

    def test_every_spelling_of_one_record_claims_one_identity(self):
        catalog = self.dukhanov()
        identities = {
            catalog.staff_id(person(f"A{index}", name))
            for index, name in enumerate(
                ("Alexey Valentinovich Dukhanov", "Aleksei Dukhanov", "Alexey Dukhanov"))
        }
        self.assertEqual(len(identities), 1)
        self.assertNotIn(None, identities)

    def test_initials_name_a_card_but_claim_no_identity(self):
        catalog = self.dukhanov()
        initials = person("A1", "A. V. Dukhanov")
        self.assertIsNone(catalog.staff_id(initials))
        self.assertIsNotNone(catalog.match(initials))

    def test_a_record_the_name_argues_against_names_nobody(self):
        # Naming is the looser question, but not this loose: the card would
        # state a full name and a degree belonging to somebody else.
        catalog = self.catalog(
            ("Дмитриев Алексей Андреевич", "Дмитриев", "Алексей", "Андреевич", "к.т.н."))
        self.assertIsNone(catalog.match(person(
            "A1", "A. D. Dmitriev", variants=["A. D. Dmitriev", "Alexey Dmitriev"])))

    def test_a_spelled_out_variant_restores_the_claim(self):
        catalog = self.dukhanov()
        self.assertEqual(
            catalog.staff_id(person("A1", "A. V. Dukhanov",
                                    variants=["A. V. Dukhanov", "Alexey Dukhanov"])),
            catalog.staff_id(person("A2", "Aleksei Dukhanov")))

    def test_a_disagreeing_middle_initial_withdraws_the_claim(self):
        # OpenAlex knows this author as "Alexey Dmitriev" too, which fits the
        # record — but A.D. is not A.A., and one of them is somebody else.
        catalog = self.catalog(
            ("Дмитриев Алексей Андреевич", "Дмитриев", "Алексей", "Андреевич", ""))
        self.assertIsNone(catalog.staff_id(person(
            "A1", "A. D. Dmitriev", variants=["A. D. Dmitriev", "Alexey Dmitriev"])))
        self.assertIsNotNone(catalog.staff_id(person(
            "A2", "A. A. Dmitriev", variants=["A. A. Dmitriev", "Alexey Dmitriev"])))

    def test_an_initial_that_folds_to_two_letters_still_agrees(self):
        # "Yu." is one initial, but folding writes it with two letters
        # ("iu") — it must still read as Юрьевич and veto nothing.
        catalog = self.catalog(
            ("Кохановский Алексей Юрьевич", "Кохановский", "Алексей", "Юрьевич", ""))
        self.assertIsNotNone(catalog.staff_id(person(
            "A1", "A. Yu. Kokhanovskiy", variants=["Alexey Kokhanovskiy"])))
        self.assertIsNotNone(catalog.staff_id(person("A2", "Alexey Y. Kokhanovsky")))

    def test_a_stray_variant_does_not_overrule_the_record_itself(self):
        # OpenAlex hangs the spellings of a mis-attributed paper on an
        # author record; "V D Kravtsov" is one, and the record is not.
        catalog = self.catalog(
            ("Кравцов Василий Андреевич", "Кравцов", "Василий", "Андреевич", ""))
        self.assertIsNotNone(catalog.staff_id(person(
            "A1", "Vasily Kravtsov",
            variants=["Kravtsov, V.", "Kravtsov, Vasily", "V D Kravtsov"])))

    def test_namesakes_in_the_catalog_claim_nothing(self):
        catalog = self.catalog(
            ("Никитин Андрей Алексеевич", "Никитин", "Андрей", "Алексеевич", ""),
            ("Никитин Андрей Викторович", "Никитин", "Андрей", "Викторович", ""))
        self.assertIsNone(catalog.staff_id(person("A1", "Andrey Nikitin")))


if __name__ == "__main__":
    unittest.main()
