import tempfile
import unittest
from pathlib import Path

from pauk.models import Person
from pauk.pipeline.stages.russian_names import RussianNamesStage, to_cyrillic
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
            ("Mikhail", "Михаил"),
            ("Shcherbakov", "Щербаков"),
            ("Tsvetkova", "Цветкова"),
            ("Petrova-Sidorova", "Петрова-Сидорова"),
            ("Wei Wang", "Веи Ванг"),
        ):
            self.assertEqual(to_cyrillic(latin), cyrillic)


if __name__ == "__main__":
    unittest.main()
