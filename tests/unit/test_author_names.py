import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.models import Person
from pauk.pipeline.stages.author_names import AuthorNamesStage, RussianNamesCatalog, to_cyrillic
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore

CATALOG_HEADER = "name_ru,surname,name,patronymic,degree\n"


def person(pid, name, *, variants=(), degree=None, itmo=True):
    return Person(id=pid, openalex_id=pid, is_itmo=itmo, name_raw=name,
                  name_variants=list(variants), degree=degree)


class _FakeOpenRouterClient:
    """Stands in for pauk.sources.OpenRouterClient in tests: returns queued
    replies in call order instead of hitting the network. A queued value of
    None simulates a failed call, matching what chat_json() returns then."""

    _queue: list = []

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.last_response = None
        self.last_usage = None
        self._replies = list(self._queue)

    def chat_json(self, prompt):
        self.last_prompt = prompt
        reply = self._replies.pop(0) if self._replies else None
        if isinstance(reply, Exception):
            raise reply
        return reply

    @classmethod
    def queued(cls, replies):
        return type("_QueuedClient", (cls,), {"_queue": replies})


class AuthorNamesStageTest(unittest.TestCase):
    def run_stage(self, people, catalog_rows, replies):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = Settings(data_dir=root)
        self.config.static_dir.mkdir(parents=True)
        (self.config.static_dir / "russian_names.csv").write_text(
            CATALOG_HEADER + "".join(f"{row}\n" for row in catalog_rows), encoding="utf-8")
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")
        self.prepared.write_models("persons", people)
        with patch("pauk.pipeline.stages.author_names.OpenRouterClient",
                   _FakeOpenRouterClient.queued(replies)):
            result = AuthorNamesStage(self.prepared, self.raw, self.config).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def test_missing_catalog_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Settings(data_dir=Path(tmp))
            db = mongomock.MongoClient()["pauk_test"]
            prepared = PreparedStore(db, "sample")
            prepared.write_models("persons", [person("A1", "Nikolay Nikitin")])
            with self.assertRaises(FileNotFoundError):
                AuthorNamesStage(prepared, RawStore(db, "sample"), config).run()

    def test_a_crash_partway_through_does_not_lose_already_processed_people(self):
        # Each person is upserted right after it's processed
        # (PreparedStore.upsert_models), not batched into one write_models()
        # call after the whole loop - so an unexpected error on person #2
        # (a malformed LLM reply, or anything else the loop doesn't guard
        # against) must not roll back the LLM work already spent and saved
        # for person #1.
        with self.assertRaises(RuntimeError):
            self.run_stage(
                [person("A1", "Nikolay Nikitin"), person("A2", "Ivan Petrov")], [],
                replies=[
                    {"matched_candidate": None, "surname_ru": "Никитин", "first_name_ru": "Николай",
                     "surname_en": "Nikitin", "first_name_en": "Nikolay", "reason": ""},
                    RuntimeError("unexpected shape"),
                ],
            )
        saved = {p.id: p for p in self.prepared.read_models("persons", Person)}
        self.assertEqual(saved["A1"].processing["author_names"].status, "completed")
        self.assertEqual(saved["A1"].surname_ru, "Никитин")
        self.assertNotIn("author_names", saved["A2"].processing)

    def test_empty_name_raw_is_completed_empty_without_an_llm_call(self):
        result, people = self.run_stage([person("A1", "")], [], replies=[])
        self.assertEqual(result["author_names"], 1)
        self.assertEqual(people["A1"].processing["author_names"].status, "completed_empty")

    def test_an_external_person_gets_the_same_llm_split_as_an_itmo_one(self):
        # The graph carries split fields for external persons too
        # (external_person prop_fields, graph/extract.py) - the stage must
        # not skip them just because the ITMO staff catalog never matches
        # a foreign co-author.
        result, people = self.run_stage(
            [person("A1", "Frank Niessen", itmo=False)], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Ниссен", "first_name_ru": "Франк",
                "surname_en": "Niessen", "first_name_en": "Frank",
                "reason": "",
            }],
        )
        self.assertEqual(result["author_names"], 1)
        row = people["A1"]
        self.assertEqual((row.surname_ru, row.first_name_ru), ("Ниссен", "Франк"))
        self.assertEqual(row.processing["author_names"].status, "completed")

    def test_llm_reply_fills_all_six_parts_and_composes_name_ru_and_name_en(self):
        result, people = self.run_stage(
            [person("A1", "Nikolay O. Nikitin")],
            ["Никитин Николай Олегович,Никитин,Николай,Олегович,к.т.н."],
            replies=[{
                "matched_candidate": 0,
                "surname_ru": "Никитин", "first_name_ru": "Николай", "second_name_ru": "Олегович",
                "surname_en": "Nikitin", "first_name_en": "Nikolay", "second_name_en": "Olegovich",
                "reason": "exact catalog match",
            }],
        )
        self.assertEqual(result["author_names"], 1)
        self.assertEqual(result["names_matched_candidate"], 1)
        row = people["A1"]
        self.assertEqual((row.surname_ru, row.first_name_ru, row.second_name_ru),
                         ("Никитин", "Николай", "Олегович"))
        self.assertEqual((row.surname_en, row.first_name_en, row.second_name_en),
                         ("Nikitin", "Nikolay", "Olegovich"))
        self.assertEqual(row.name_ru, "Никитин Николай Олегович")
        self.assertEqual(row.name_en, "Nikitin Nikolay Olegovich")
        # degree comes from the free deterministic catalog lookup, not the LLM.
        self.assertEqual(row.degree, "к.т.н.")
        self.assertEqual(row.processing["author_names"].status, "completed")

    def test_existing_degree_is_not_overwritten(self):
        _, people = self.run_stage(
            [person("A1", "Nikolay O. Nikitin", degree="PhD")],
            ["Никитин Николай Олегович,Никитин,Николай,Олегович,к.т.н."],
            replies=[{"matched_candidate": 0, "surname_ru": "Никитин", "first_name_ru": "Николай",
                      "second_name_ru": "Олегович", "surname_en": "Nikitin", "first_name_en": "Nikolay",
                      "second_name_en": "Olegovich", "reason": ""}],
        )
        self.assertEqual(people["A1"].degree, "PhD")

    def test_a_failed_llm_call_falls_back_to_transliteration_for_name_ru_and_name_raw_for_name_en(self):
        result, people = self.run_stage([person("A1", "Pavel V. Zhukov")], [], replies=[None])
        self.assertEqual(result["names_failed"], 1)
        row = people["A1"]
        self.assertEqual(row.name_ru, to_cyrillic("Pavel V. Zhukov"))
        self.assertEqual(row.name_en, "Pavel V. Zhukov")  # name_raw is already Latin
        self.assertIsNone(row.surname_ru)  # parts are never guessed on failure
        self.assertEqual(row.processing["author_names"].status, "failed")

    def test_a_failed_llm_call_does_not_erase_a_name_ru_an_earlier_run_wrote(self):
        first = person("A1", "Pavel V. Zhukov")
        first.name_ru = "Павел Жуков"
        first.name_en = "Pavel Zhukov"
        _, people = self.run_stage([first], [], replies=[None])
        self.assertEqual(people["A1"].name_ru, "Павел Жуков")
        self.assertEqual(people["A1"].name_en, "Pavel Zhukov")

    def test_an_invented_patronymic_with_no_candidate_is_dropped(self):
        # The model states a patronymic despite there being no directory
        # match and nothing in the input spelling it out - exactly the
        # qwen3.7-flash failure this guard exists for.
        result, people = self.run_stage(
            [person("A1", "Valentine G. Nenajdenko")], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Ненайденко", "first_name_ru": "Валентин", "second_name_ru": "Геннадьевич",
                "surname_en": "Nenajdenko", "first_name_en": "Valentine", "second_name_en": "Gennadievich",
                "reason": "standard expansion of G.",
            }],
        )
        self.assertEqual(result["names_second_name_corrected"], 1)
        row = people["A1"]
        self.assertIsNone(row.second_name_ru)
        self.assertIsNone(row.second_name_en)
        self.assertEqual(row.surname_ru, "Ненайденко")  # the rest of the reply is kept

    def test_a_patronymic_spelled_out_in_a_variant_is_kept(self):
        result, people = self.run_stage(
            [person("A1", "I. Ivanova", variants=["Irina Anatolievna Ivanova"])], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Иванова", "first_name_ru": "Ирина", "second_name_ru": "Анатольевна",
                "surname_en": "Ivanova", "first_name_en": "Irina", "second_name_en": "Anatolievna",
                "reason": "spelled out in a known variant",
            }],
        )
        self.assertEqual(result["names_second_name_corrected"], 0)
        self.assertEqual(people["A1"].second_name_en, "Anatolievna")

    def test_a_patronymic_backed_by_a_matched_candidate_is_trusted(self):
        result, people = self.run_stage(
            [person("A1", "M.V. Dorogov")],
            ["Дорогов Максим Владимирович,Дорогов,Максим,Владимирович,"],
            replies=[{
                "matched_candidate": 0,
                "surname_ru": "Дорогов", "first_name_ru": "Максим", "second_name_ru": "Владимирович",
                "surname_en": "Dorogov", "first_name_en": "Maxim", "second_name_en": "Vladimirovich",
                "reason": "initials match the directory record",
            }],
        )
        self.assertEqual(result["names_second_name_corrected"], 0)
        self.assertEqual(people["A1"].second_name_en, "Vladimirovich")

    def test_a_bare_initial_second_name_survives_the_guard(self):
        _, people = self.run_stage(
            [person("A1", "A. I. Marchenko")], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Марченко", "first_name_ru": "А", "second_name_ru": "И",
                "surname_en": "Marchenko", "first_name_en": "A", "second_name_en": "I",
                "reason": "only initials given",
            }],
        )
        self.assertEqual(people["A1"].second_name_ru, "И")
        self.assertEqual(people["A1"].second_name_en, "I")

    def test_a_second_given_name_is_folded_back_out_of_the_patronymic_slot(self):
        # Spanish naming has no patronymic - "Luis" is a second given name,
        # not one. Found for real: qwen put "Kumar" (Ripon Kumar Adhikary)
        # and "Opoku" (Bright Opoku Ahinkorah) here across live test runs.
        _, people = self.run_stage(
            [person("A1", "Pedro Luis Gonzalez")], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Гонзалез", "first_name_ru": "Педро", "second_name_ru": "Луис",
                "surname_en": "Gonzalez", "first_name_en": "Pedro", "second_name_en": "Luis",
                "reason": "extracted three name-position words",
            }],
        )
        row = people["A1"]
        self.assertEqual((row.first_name_ru, row.second_name_ru), ("Педро Луис", None))
        self.assertEqual((row.first_name_en, row.second_name_en), ("Pedro Luis", None))

    def test_a_second_name_that_carries_a_period_is_still_recognised_as_an_initial(self):
        # The prompt asks for a bare letter, no period - real replies don't
        # always comply (found across live runs: "N.", "E.", "I."). Without
        # tolerating the period, this guard would misfire on a legitimate
        # patronymic initial.
        _, people = self.run_stage(
            [person("A1", "Mikhailov N.N.")], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Михайлов", "first_name_ru": "Н.", "second_name_ru": "Н.",
                "surname_en": "Mikhailov", "first_name_en": "N.", "second_name_en": "N.",
                "reason": "only initials given",
            }],
        )
        self.assertEqual(people["A1"].second_name_ru, "Н.")
        self.assertEqual(people["A1"].second_name_en, "N.")

    def test_a_real_patronymic_survives_the_shape_guard(self):
        _, people = self.run_stage(
            [person("A1", "Ivan Petrovich Sidorov")], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Сидоров", "first_name_ru": "Иван", "second_name_ru": "Петрович",
                "surname_en": "Sidorov", "first_name_en": "Ivan", "second_name_en": "Petrovich",
                "reason": "spelled out in full",
            }],
        )
        self.assertEqual(people["A1"].second_name_ru, "Петрович")
        self.assertEqual(people["A1"].second_name_en, "Petrovich")

    def test_a_broken_partial_transliteration_is_dropped(self):
        # An unusual Latin character (Polish "ł") defeats the model mid-word
        # instead of failing outright.
        _, people = self.run_stage(
            [person("A1", "Małgorzata Konopka")], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Конопка", "first_name_ru": "Маłgorzata", "second_name_ru": None,
                "surname_en": "Konopka", "first_name_en": "Małgorzata", "second_name_en": None,
                "reason": "transcribed to Russian conventionally",
            }],
        )
        row = people["A1"]
        self.assertIsNone(row.first_name_ru)
        self.assertEqual(row.surname_ru, "Конопка")  # fully Cyrillic, untouched
        self.assertEqual(row.first_name_en, "Małgorzata")  # English field is not guarded

    def test_a_bare_latin_initial_in_a_ru_field_is_transliterated_not_dropped(self):
        _, people = self.run_stage(
            [person("A1", "I. А. Zelinskaya")], [],
            replies=[{
                "matched_candidate": None,
                "surname_ru": "Зелинская", "first_name_ru": "I", "second_name_ru": "A",
                "surname_en": "Zelinskaya", "first_name_en": "I", "second_name_en": "A",
                "reason": "only initials given",
            }],
        )
        self.assertEqual(people["A1"].first_name_ru, "И")
        self.assertEqual(people["A1"].second_name_ru, "А")

    def test_second_run_is_a_no_op(self):
        self.run_stage(
            [person("A1", "Pavel Zhukov")], [],
            replies=[{"matched_candidate": None, "surname_ru": "Жуков", "first_name_ru": "Павел",
                      "second_name_ru": None, "surname_en": "Zhukov", "first_name_en": "Pavel",
                      "second_name_en": None, "reason": ""}],
        )
        with patch("pauk.pipeline.stages.author_names.OpenRouterClient",
                   _FakeOpenRouterClient.queued([])):
            again = AuthorNamesStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(again["author_names"], 0)


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
            dict(zip(("name_ru", "surname", "name", "patronymic", "degree"), row, strict=True))
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
