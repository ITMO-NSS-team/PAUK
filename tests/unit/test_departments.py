import json
import tempfile
import unittest
from pathlib import Path

from pauk.models import Department, Person, Publication, School
from pauk.models.processing import ProcessingStatus
from pauk.models.relations import Authorship
from pauk.pipeline.stages.departments import DepartmentsStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


def _prepare(root: Path, persons, publications) -> PreparedStore:
    prepared = PreparedStore(root / "prepared", "sample")
    prepared.write_models("persons", persons)
    prepared.write_models("publications", publications)
    return prepared


def _run(root: Path, departments, persons, publications):
    static = root / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "departments.jsonl").write_text("\n".join(d.model_dump_json() for d in departments), encoding="utf-8")
    prepared = _prepare(root, persons, publications)
    DepartmentsStage(prepared, RawStore(root / "raw", "sample"), config=Settings(data_dir=root)).run()
    return (
        {p.id: p for p in prepared.read_models("persons", Person)},
        {p.id: p for p in prepared.read_models("publications", Publication)},
    )


def _run_catalog(root: Path, catalog: list[dict], persons, publications) -> PreparedStore:
    static = root / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "departments_catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    prepared = _prepare(root, persons, publications)
    DepartmentsStage(prepared, RawStore(root / "raw", "sample"), config=Settings(data_dir=root)).run()
    return prepared


class DepartmentsStageTest(unittest.TestCase):
    def test_matches_english_name_and_propagates_to_itmo_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="Faculty of Physics, ITMO University"),
                ],
            )
            persons, pubs = _run(
                root,
                [Department(id="d1", name_en="Faculty of Physics")],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d1"])
            self.assertEqual(pubs["W1"].department_ids, ["d1"])
            self.assertEqual(persons["P1"].processing["departments"].status, ProcessingStatus.COMPLETED)

    def test_matches_numbered_affiliation_prefix(self):
        # Multi-affiliation papers glue an index to the name ("2School of ...").
        # Substring matching keeps these; word-boundary matching (tried, reverted)
        # dropped them because the digit is a word character.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="1Some Institute, 2School of Physics and Engineering"),
                ],
            )
            persons, _ = _run(
                root,
                [Department(id="d1", name_en="School of Physics and Engineering")],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d1"])

    def test_matches_russian_name_for_cyrillic_affiliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="Физический факультет, Университет ИТМО"),
                ],
            )
            persons, _ = _run(
                root,
                [Department(id="d1", name_en="Faculty of Physics", name_ru="Физический факультет")],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d1"])

    def test_matches_name_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="FBIT, ITMO University"),
                ],
            )
            persons, _ = _run(
                root,
                [Department(id="d1", name_en="Faculty of Secure Information Technologies", name_variants=["FBIT"])],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d1"])


class DepartmentHierarchyTest(unittest.TestCase):
    def test_catalog_derives_school_id_and_emits_school_nodes(self):
        # Two units under one school_en → both departments share one school_id,
        # and a single School node is emitted for the graph PART_OF edge.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "school_en": "School of X",
                    "school_ru": "Школа X",
                    "name_en": "School of X",
                    "name_ru": "Школа X",
                    "aliases": [],
                },
                {
                    "school_en": "School of X",
                    "school_ru": "Школа X",
                    "name_en": "Faculty of Y",
                    "name_ru": "Факультет Y",
                    "aliases": [],
                },
            ]
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="Faculty of Y, ITMO University"),
                ],
            )
            prepared = _run_catalog(root, catalog, [person], [Publication(id="W1", title="t")])

            departments = {d.name_en: d for d in prepared.read_models("departments", Department)}
            schools = list(prepared.read_models("schools", School))
            faculty = departments["Faculty of Y"]

            self.assertIsNotNone(faculty.school_id)
            self.assertEqual(faculty.school_id, departments["School of X"].school_id)
            self.assertEqual([s.name_en for s in schools], ["School of X"])
            self.assertEqual(schools[0].id, faculty.school_id)
            self.assertEqual(schools[0].name_ru, "Школа X")

    def test_units_without_school_have_no_school_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "school_en": "",
                    "school_ru": "",
                    "name_en": "Standalone Lab",
                    "name_ru": "Лаборатория",
                    "aliases": [],
                }
            ]
            prepared = _run_catalog(root, catalog, [], [])
            departments = list(prepared.read_models("departments", Department))
            self.assertEqual(departments[0].school_id, None)
            self.assertEqual(list(prepared.read_models("schools", School)), [])


if __name__ == "__main__":
    unittest.main()
