import json
import tempfile
import unittest
from pathlib import Path

from pauk.graph.extract import NODE_REGISTRY, extract_relationships
from pauk.models import Department, Organization, Person, Publication
from pauk.models.processing import ProcessingStatus
from pauk.models.relations import Authorship
from pauk.pipeline.stages.departments import DepartmentsStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from pauk.storage.static import StaticStore


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

    def test_matches_across_embedded_newline(self):
        # PDF/OpenAlex affiliations wrap a unit name across a line break; without
        # whitespace-collapse "School of\nPhysics" would silently miss the catalogue
        # name "School of Physics" (the catalogue side is normalised the same way).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="School of\nPhysics,  ITMO   University"),
                ],
            )
            persons, _ = _run(
                root,
                [Department(id="d1", name_en="School of Physics")],
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

    def test_no_match_records_completed_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[Authorship(publication_id="W1", affiliation="Some Foreign University, UK")],
            )
            persons, _ = _run(
                root,
                [Department(id="d1", name_en="Faculty of Physics")],
                [person],
                [Publication(id="W1", title="t")],
            )
            state = persons["P1"].processing["departments"]
            self.assertEqual(persons["P1"].department_ids, [])
            self.assertEqual(state.status, ProcessingStatus.COMPLETED_EMPTY)
            self.assertEqual(state.result_count, 0)

    def test_catalog_alias_becomes_name_variant_and_matches(self):
        # aliases in the catalogue are loaded as name_variants (static.py) and then
        # drive matching — exercise the two together, not just a hand-built model.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "uid": "fac-sit",
                    "name_en": "Faculty of Secure Information Technologies",
                    "name_ru": "",
                    "kind": "faculty",
                    "parent": None,
                    "aliases": ["FBIT"],
                }
            ]
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[Authorship(publication_id="W1", affiliation="FBIT, ITMO University")],
            )
            prepared = _run_catalog(root, catalog, [person], [Publication(id="W1", title="t")])
            departments = {d.name_en: d for d in prepared.read_models("departments", Department)}
            self.assertEqual(departments["Faculty of Secure Information Technologies"].name_variants, ["FBIT"])
            matched = {p.id: p for p in prepared.read_models("persons", Person)}["P1"]
            self.assertTrue(matched.department_ids)


class DepartmentHierarchyTest(unittest.TestCase):
    def test_top_unit_links_to_organization_subunit_to_parent(self):
        # A megafaculty is PART_OF the Organization (organization_id); a faculty
        # under it is PART_OF that megafaculty Department (parent_id).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "uid": "itmo",
                    "name_en": "ITMO University",
                    "name_ru": "Университет ИТМО",
                    "kind": "organization",
                    "parent": None,
                },
                {
                    "uid": "school-x",
                    "name_en": "School of X",
                    "name_ru": "Школа X",
                    "kind": "megafaculty",
                    "parent": "itmo",
                },
                {
                    "uid": "faculty-y",
                    "name_en": "Faculty of Y",
                    "name_ru": "Факультет Y",
                    "kind": "faculty",
                    "parent": "school-x",
                },
            ]
            prepared = _run_catalog(root, catalog, [], [])
            d = {x.name_en: x for x in prepared.read_models("departments", Department)}
            orgs = list(prepared.read_models("organizations", Organization))

            self.assertEqual([o.name_en for o in orgs], ["ITMO University"])
            self.assertNotIn("ITMO University", d)  # the org is not a Department
            school = d["School of X"]
            self.assertEqual(school.organization_id, orgs[0].id)
            self.assertIsNone(school.parent_id)
            faculty = d["Faculty of Y"]
            self.assertEqual(faculty.parent_id, school.id)
            self.assertIsNone(faculty.organization_id)

    def test_multi_level_chain_resolves_each_parent(self):
        # organization -> megafaculty -> faculty -> department, each linking to
        # the level directly above (org via organization_id, rest via parent_id).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "uid": "itmo",
                    "name_en": "ITMO University",
                    "name_ru": "Университет ИТМО",
                    "kind": "organization",
                    "parent": None,
                },
                {
                    "uid": "school-x",
                    "name_en": "School of X",
                    "name_ru": "Школа X",
                    "kind": "megafaculty",
                    "parent": "itmo",
                },
                {
                    "uid": "faculty-y",
                    "name_en": "Faculty of Y",
                    "name_ru": "Факультет Y",
                    "kind": "faculty",
                    "parent": "school-x",
                },
                {
                    "uid": "dept-z",
                    "name_en": "Department of Z",
                    "name_ru": "Кафедра Z",
                    "kind": "department",
                    "parent": "faculty-y",
                },
            ]
            prepared = _run_catalog(root, catalog, [], [])
            d = {x.name_en: x for x in prepared.read_models("departments", Department)}
            org = list(prepared.read_models("organizations", Organization))[0]
            self.assertEqual(d["School of X"].organization_id, org.id)
            self.assertEqual(d["Faculty of Y"].parent_id, d["School of X"].id)
            self.assertEqual(d["Department of Z"].parent_id, d["Faculty of Y"].id)

    def test_unknown_parent_uid_raises(self):
        # A parent uid that names no entry is a catalogue typo — fail loudly rather
        # than silently orphan the unit (its PART_OF edge would just drop at load).
        with tempfile.TemporaryDirectory() as tmp:
            static = Path(tmp) / "static"
            static.mkdir(parents=True)
            (static / "departments_catalog.json").write_text(
                json.dumps(
                    [
                        {"uid": "itmo", "name_en": "ITMO University", "kind": "organization", "parent": None},
                        {"uid": "faculty-y", "name_en": "Faculty of Y", "kind": "faculty", "parent": "typo-uid"},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                StaticStore(static).departments()

    def test_catalog_chain_materialises_graph_edges(self):
        # Cross the seam static.py -> extract.py: a 3-level catalogue chain must
        # yield the matching PART_OF edges (Department->Department, Department->Organization).
        with tempfile.TemporaryDirectory() as tmp:
            static = Path(tmp) / "static"
            static.mkdir(parents=True)
            (static / "departments_catalog.json").write_text(
                json.dumps(
                    [
                        {"uid": "itmo", "name_en": "ITMO University", "kind": "organization", "parent": None},
                        {"uid": "school-x", "name_en": "School of X", "kind": "megafaculty", "parent": "itmo"},
                        {"uid": "faculty-y", "name_en": "Faculty of Y", "kind": "faculty", "parent": "school-x"},
                    ]
                ),
                encoding="utf-8",
            )
            edges: dict = {}
            for dept in StaticStore(static).departments():
                for key, rels in extract_relationships(dept.model_dump(), NODE_REGISTRY["department"]).items():
                    edges.setdefault(key, []).extend(rels)
            self.assertEqual(edges[("Department", "Organization", "PART_OF", "id")], [("school-x", "itmo", {})])
            self.assertEqual(edges[("Department", "Department", "PART_OF", "id")], [("faculty-y", "school-x", {})])

    def test_organization_is_separate_node_and_not_matched(self):
        # The root organisation is emitted as an Organization, never a Department,
        # so its name cannot attach an author to it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "uid": "itmo",
                    "name_en": "ITMO University",
                    "name_ru": "Университет ИТМО",
                    "kind": "organization",
                    "parent": None,
                    "ror_id": "https://ror.org/04txgxn49",
                    "country": "Russia",
                    "type": "university",
                },
                {
                    "uid": "faculty-y",
                    "name_en": "Faculty of Y",
                    "name_ru": "Факультет Y",
                    "kind": "faculty",
                    "parent": "itmo",
                },
            ]
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[Authorship(publication_id="W1", affiliation="ITMO University, Saint Petersburg")],
            )
            prepared = _run_catalog(root, catalog, [person], [Publication(id="W1", title="t")])
            org = list(prepared.read_models("organizations", Organization))[0]
            self.assertEqual(org.ror_id, "https://ror.org/04txgxn49")
            self.assertEqual(org.country, "Russia")
            self.assertEqual(org.type, "university")
            matched = {p.id: p for p in prepared.read_models("persons", Person)}["P1"]
            self.assertEqual(matched.department_ids, [])


class DepartmentContextAliasTest(unittest.TestCase):
    def _dept(self) -> Department:
        return Department(id="d1", name_en="Faculty of Physics", context_aliases=["Department of Physics"])

    def _person(self, affiliation: str) -> Person:
        return Person(
            id="P1",
            is_itmo=True,
            authored=[Authorship(publication_id="W1", affiliation=affiliation)],
        )

    def _match(self, root: Path, affiliation: str) -> list[str]:
        persons, _ = _run(root, [self._dept()], [self._person(affiliation)], [Publication(id="W1", title="t")])
        return persons["P1"].department_ids

    def test_context_alias_matches_inside_itmo_segment(self):
        # "Department of Physics" is generic; it matches when its segment names ITMO.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._match(Path(tmp), "Department of Physics, ITMO University, Saint Petersburg"),
                ["d1"],
            )

    def test_context_alias_ignored_in_foreign_segment(self):
        # The same generic name in a non-ITMO segment must not match a co-affiliation.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), "Department of Physics, University of Oxford, UK"), [])

    def test_context_alias_not_matched_across_segment_boundary(self):
        # An ITMO marker in a different segment does not license the generic name.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._match(Path(tmp), "Department of Physics, University of Oxford\nITMO University"),
                [],
            )

    def test_context_alias_matches_when_itmo_marker_precedes(self):
        # Org-first order: the ITMO marker in the previous part still licenses it.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), "ITMO University, Department of Physics"), ["d1"])

    def test_context_alias_matches_in_same_part_as_marker(self):
        # No comma between the name and the org — both sit in one part.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), "Department of Physics ITMO University, Saint Petersburg"), ["d1"])

    def test_context_alias_isolated_across_affiliations(self):
        # A generic alias in one authorship must not borrow an ITMO marker from a
        # different authorship — parts are collected per affiliation.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_itmo=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="Department of Physics, University of Oxford"),
                    Authorship(publication_id="W2", affiliation="ITMO University, Saint Petersburg"),
                ],
            )
            persons, _ = _run(
                root,
                [self._dept()],
                [person],
                [Publication(id="W1", title="t"), Publication(id="W2", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, [])

    def test_context_alias_prefers_longest_match_in_part(self):
        # "Department of Physics" must not fire inside "Department of Physics and
        # Engineering" — only the more specific unit is credited for that part.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = self._person("Department of Physics and Engineering, ITMO University")
            persons, _ = _run(
                root,
                [
                    Department(id="phys", name_en="Faculty of Physics", context_aliases=["Department of Physics"]),
                    Department(
                        id="school",
                        name_en="School of Physics and Engineering",
                        context_aliases=["Department of Physics and Engineering"],
                    ),
                ],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["school"])

    def test_context_alias_loaded_from_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "uid": "fac-phys",
                    "name_en": "Faculty of Physics",
                    "name_ru": "Факультет физики",
                    "kind": "faculty",
                    "parent": None,
                    "aliases": [],
                    "context_aliases": ["Department of Physics"],
                }
            ]
            person = self._person("Department of Physics, ITMO University")
            prepared = _run_catalog(root, catalog, [person], [Publication(id="W1", title="t")])
            departments = {d.name_en: d for d in prepared.read_models("departments", Department)}
            self.assertEqual(departments["Faculty of Physics"].context_aliases, ["Department of Physics"])
            matched = {p.id: p for p in prepared.read_models("persons", Person)}["P1"]
            self.assertTrue(matched.department_ids)


if __name__ == "__main__":
    unittest.main()
