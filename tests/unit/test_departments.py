import json
import tempfile
import unittest
from pathlib import Path

import mongomock

from pauk.graph.extract import NODE_REGISTRY, extract_relationships
from pauk.models import Department, Organization, Person, Publication
from pauk.models.processing import ProcessingStatus
from pauk.models.relations import Authorship
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.departments import DepartmentsStage, _contains, _normalize
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from pauk.storage.static import StaticStore


def _prepare(db, persons, publications) -> PreparedStore:
    prepared = PreparedStore(db, "sample")
    prepared.write_models("persons", persons)
    prepared.write_models("publications", publications)
    return prepared


def _run(root: Path, departments, persons, publications):
    static = root / "static" / "itmo"
    static.mkdir(parents=True, exist_ok=True)
    (static / "departments.jsonl").write_text("\n".join(d.model_dump_json() for d in departments), encoding="utf-8")
    db = mongomock.MongoClient()["pauk_test"]
    prepared = _prepare(db, persons, publications)
    DepartmentsStage(prepared, RawStore(db, "sample"), config=Settings(data_dir=root)).run()
    return (
        {p.id: p for p in prepared.read_models("persons", Person)},
        {p.id: p for p in prepared.read_models("publications", Publication)},
    )


def _run_catalog(root: Path, catalog: list[dict], persons, publications) -> PreparedStore:
    static = root / "static" / "itmo"
    static.mkdir(parents=True, exist_ok=True)
    (static / "departments_catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    db = mongomock.MongoClient()["pauk_test"]
    prepared = _prepare(db, persons, publications)
    DepartmentsStage(prepared, RawStore(db, "sample"), config=Settings(data_dir=root)).run()
    return prepared


class DepartmentsStageTest(unittest.TestCase):
    def test_matches_english_name_and_propagates_to_itmo_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_member=True,
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
        # A digit before the name does not count as a word start.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_member=True,
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
                is_member=True,
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
                is_member=True,
                authored=[
                    Authorship(publication_id="W1", affiliation="Физический факультет, Университет ИТМО"),
                ],
            )
            persons, _ = _run(
                root,
                [Department(id="d1", name_en="Faculty of Physics", name_local="Физический факультет")],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d1"])

    def test_matches_name_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_member=True,
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

    def test_one_word_name_not_matched_inside_longer_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = [
                Person(
                    id=f"P{n}",
                    is_member=True,
                    authored=[Authorship(publication_id="W1", affiliation=affiliation)],
                )
                for n, affiliation in enumerate(["JSC MorFBITs, Saint Petersburg", "ITMO University; FBITs Group"])
            ]
            persons, _ = _run(
                Path(tmp),
                [Department(id="d1", name_en="Faculty of Secure Information Technologies", name_variants=["FBIT"])],
                people,
                [Publication(id="W1", title="t")],
            )
            self.assertEqual([persons[f"P{n}"].department_ids for n in (0, 1)], [[], []])

    def test_name_ending_in_short_word_not_matched_inside_longer_acronym(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = Person(
                id="P1",
                is_member=True,
                authored=[Authorship(publication_id="W1", affiliation="Институт ИИТ, Университет ИТМО")],
            )
            persons, _ = _run(
                Path(tmp),
                [Department(id="d1", name_en="Institute for Artificial Intelligence", name_variants=["Институт ИИ"])],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, [])

    def test_one_word_name_matches_next_to_digits_and_brackets(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = [
                Person(
                    id="P1",
                    is_member=True,
                    authored=[Authorship(publication_id="W1", affiliation="2SCAMT Institute, ITMO University")],
                ),
                Person(
                    id="P2",
                    is_member=True,
                    authored=[Authorship(publication_id="W1", affiliation="Robotics (BE2R) Lab, ITMO University")],
                ),
            ]
            persons, _ = _run(
                Path(tmp),
                [
                    Department(id="d1", name_en="SCAMT Institute ITMO", name_variants=["SCAMT"]),
                    Department(id="d2", name_en="Laboratory of Biomechatronics", name_variants=["BE2R"]),
                ],
                people,
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d1"])
            self.assertEqual(persons["P2"].department_ids, ["d2"])

    def test_name_not_matched_at_end_of_longer_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = Person(
                id="P1",
                is_member=True,
                authored=[Authorship(publication_id="W1", affiliation="Megafaculty of Photonics, ITMO University")],
            )
            persons, _ = _run(
                Path(tmp),
                [
                    Department(id="d1", name_en="Center for Photonics", name_variants=["Faculty of Photonics"]),
                    Department(
                        id="d2",
                        name_en="School of Physics and Engineering",
                        name_variants=["Megafaculty of Photonics"],
                    ),
                ],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d2"])

    def _matched(self, departments: list[Department], affiliations: list[str]) -> list[list[str]]:
        people = [
            Person(id=f"P{n}", is_member=True, authored=[Authorship(publication_id="W1", affiliation=affiliation)])
            for n, affiliation in enumerate(affiliations)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            persons, _ = _run(Path(tmp), departments, people, [Publication(id="W1", title="t")])
        return [persons[f"P{n}"].department_ids for n in range(len(affiliations))]

    def test_name_ending_in_a_three_letter_word_matches_its_plural(self):
        self.assertEqual(
            self._matched(
                [Department(id="d1", name_en="Machine Learning Lab")], ["Machine Learning Labs, ITMO University"]
            ),
            [["d1"]],
        )

    def test_multiword_name_matches_a_longer_last_word(self):
        self.assertEqual(
            self._matched(
                [Department(id="d1", name_en="Institute of Life Science")],
                ["Institute of Life Sciences, ITMO University"],
            ),
            [["d1"]],
        )

    def test_apostrophes_inside_a_word_are_ignored(self):
        self.assertEqual(
            self._matched(
                [Department(id="d1", name_en="Physics School", name_variants=["Megafakul'tet fotoniki"])],
                [
                    "Megafakul’tet fotoniki, ITMO University",
                    "Megafakul�tet fotoniki, ITMO University",
                    "Megafakultet fotoniki, ITMO University",
                    "Megafakul´tet fotoniki, ITMO University",
                    "Megafakul‘tet fotoniki, ITMO University",
                ],
            ),
            [["d1"], ["d1"], ["d1"], ["d1"], ["d1"]],
        )

    def test_html_entities_in_affiliation_are_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            person = Person(
                id="P1",
                is_member=True,
                authored=[
                    Authorship(
                        publication_id="W1",
                        affiliation="Educational Center &#x201C;Energy Efficient Engineering Systems&#x201D;, ITMO",
                    )
                ],
            )
            persons, _ = _run(
                Path(tmp),
                [Department(id="d1", name_en='Educational Center "Energy-Efficient Engineering Systems"')],
                [person],
                [Publication(id="W1", title="t")],
            )
            self.assertEqual(persons["P1"].department_ids, ["d1"])

    def test_no_match_records_completed_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_member=True,
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
        # drive matching - exercise the two together, not just a hand-built model.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = [
                {
                    "uid": "fac-sit",
                    "name_en": "Faculty of Secure Information Technologies",
                    "name_local": "",
                    "kind": "faculty",
                    "parent": None,
                    "aliases": ["FBIT"],
                }
            ]
            person = Person(
                id="P1",
                is_member=True,
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
                    "name_local": "Университет ИТМО",
                    "kind": "organization",
                    "parent": None,
                },
                {
                    "uid": "school-x",
                    "name_en": "School of X",
                    "name_local": "Школа X",
                    "kind": "megafaculty",
                    "parent": "itmo",
                },
                {
                    "uid": "faculty-y",
                    "name_en": "Faculty of Y",
                    "name_local": "Факультет Y",
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
                    "name_local": "Университет ИТМО",
                    "kind": "organization",
                    "parent": None,
                },
                {
                    "uid": "school-x",
                    "name_en": "School of X",
                    "name_local": "Школа X",
                    "kind": "megafaculty",
                    "parent": "itmo",
                },
                {
                    "uid": "faculty-y",
                    "name_en": "Faculty of Y",
                    "name_local": "Факультет Y",
                    "kind": "faculty",
                    "parent": "school-x",
                },
                {
                    "uid": "dept-z",
                    "name_en": "Department of Z",
                    "name_local": "Кафедра Z",
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
        # A parent uid that names no entry is a catalogue typo - fail loudly rather
        # than silently orphan the unit (its PART_OF edge would just drop at load).
        with tempfile.TemporaryDirectory() as tmp:
            static = Path(tmp) / "static" / "itmo"
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
            static = Path(tmp) / "static" / "itmo"
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
                    "name_local": "Университет ИТМО",
                    "kind": "organization",
                    "parent": None,
                    "ror_id": "https://ror.org/04txgxn49",
                    "country": "Russia",
                    "type": "university",
                },
                {
                    "uid": "faculty-y",
                    "name_en": "Faculty of Y",
                    "name_local": "Факультет Y",
                    "kind": "faculty",
                    "parent": "itmo",
                },
            ]
            person = Person(
                id="P1",
                is_member=True,
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
            is_member=True,
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

    def test_generic_official_name_listed_as_context_alias_matches_only_next_to_itmo(self):
        department = Department(
            id="d1", name_en="Institute of Mathematics", context_aliases=["Institute of Mathematics"]
        )
        people = [
            self._person("Steklov Institute of Mathematics, Saint Petersburg"),
            Person(
                id="P2",
                is_member=True,
                authored=[Authorship(publication_id="W1", affiliation="Institute of Mathematics, ITMO University")],
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            persons, _ = _run(Path(tmp), [department], people, [Publication(id="W1", title="t")])
        self.assertEqual([persons["P1"].department_ids, persons["P2"].department_ids], [[], ["d1"]])

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
        # No comma between the name and the org - both sit in one part.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), "Department of Physics ITMO University, Saint Petersburg"), ["d1"])

    def test_context_alias_isolated_across_affiliations(self):
        # A generic alias in one authorship must not borrow an ITMO marker from a
        # different authorship - parts are collected per affiliation.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            person = Person(
                id="P1",
                is_member=True,
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
        # Engineering" - only the more specific unit is credited for that part.
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
                    "name_local": "Факультет физики",
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

    def _match_with(self, root: Path, departments: list[Department], affiliation: str) -> list[str]:
        persons, _ = _run(root, departments, [self._person(affiliation)], [Publication(id="W1", title="t")])
        return persons["P1"].department_ids

    def test_context_alias_not_matched_inside_a_longer_word(self):
        department = Department(id="d1", name_en="Physics Unit", context_aliases=["Faculty of Physics"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match_with(Path(tmp), [department], "Megafaculty of Physics, ITMO University"), [])

    def test_context_alias_rejected_when_another_university_shares_the_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = [
                "Department of Physics St. Petersburg State University; ITMO University",
                "Department of Physics, Saint Petersburg State University, ITMO University",
            ]
            self.assertEqual([self._match(root, case) for case in cases], [[], []])

    def test_context_alias_matches_two_parts_from_itmo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._match(Path(tmp), "Department of Physics, Laboratory of Optics, ITMO University"), ["d1"]
            )

    def test_academic_titles_and_empty_parts_do_not_count_towards_the_distance(self):
        affiliation = (
            "Department of Physics, Doctor of Technical Sciences, Professor, , ITMO University, Saint Petersburg"
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), affiliation), ["d1"])

    def test_long_itmo_name_split_by_a_comma_is_a_marker(self):
        affiliation = (
            "Department of Physics, Saint Petersburg National Research University of Information Technologies, "
            "Mechanics and Optics, Saint Petersburg"
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), affiliation), ["d1"])

    def test_long_itmo_name_without_a_comma_is_a_marker(self):
        affiliation = "Department of Physics, University of Information Technologies Mechanics and Optics"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), affiliation), ["d1"])

    def test_cyrillic_marker_not_found_inside_a_word(self):
        department = Department(id="d1", name_en="Math Unit", context_aliases=["Кафедра высшей математики"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [
                    self._match_with(root, [department], "Кафедра высшей математики, кафедра алгоритмов"),
                    self._match_with(root, [department], "Кафедра высшей математики, СПбГУ"),
                    self._match_with(root, [department], "Кафедра высшей математики, Университет ИТМО"),
                ],
                [[], [], ["d1"]],
            )

    def test_plain_name_inside_a_longer_context_name_does_not_count(self):
        departments = [
            Department(id="phys", name_en="Faculty of Physics"),
            Department(
                id="school",
                name_en="School of Physics and Engineering",
                context_aliases=["Faculty of Physics and Engineering"],
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [
                    self._match_with(root, departments, "Faculty of Physics and Engineering, ITMO University"),
                    self._match_with(
                        root,
                        departments,
                        "Faculty of Physics and Engineering, ITMO University; Faculty of Physics, ITMO University",
                    ),
                ],
                [["school"], ["phys", "school"]],
            )

    def test_context_alias_two_parts_away_needs_a_sub_unit_or_the_city_in_between(self):
        rejected = [
            "Department of Physics, SPbU, ITMO University",
            "Department of Physics, Ioffe Institute, ITMO University",
            "Department of Physics, Russian Quantum Center, ITMO University",
            "Department of Physics, HSE; ITMO University",
            "Department of Physics, Professor, MSU, ITMO University",
            "ITMO University; Qingdao Innovation and Development Center; Department of Physics",
            "Department of Physics, Research Center Juelich; ITMO University",
            "Department of Physics, Group of Companies, ITMO University",
            "Department of Physics, Los Alamos National Laboratory; ITMO University",
            "Department of Physics, Nokia Bell Labs, ITMO University",
        ]
        accepted = [
            "Department of Physics, Laboratory of Optics, ITMO University",
            "Department of Physics, Scientific and Educational Laboratory of Optics, ITMO University",
            "Department of Physics, C-Vision Lab, ITMO University",
            "ITMO University, Saint-Petersburg, Department of Physics",
            "ITMO University, St, Department of Physics, Petersburg, Russia",
            "Department of Physics, Saint–Petersburg, ITMO University",
            "Department of Physics, г. Санкт-Петербург, Университет ИТМО",
            "ITMO University, Kronversky Pr. 49, Department of Physics",
            "Department of Physics, Senior Researcher, ITMO University",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [self._match(root, case) for case in rejected + accepted],
                [[]] * len(rejected) + [["d1"]] * len(accepted),
            )

    def test_catalogue_unit_between_a_context_alias_and_itmo_keeps_them_together(self):
        departments = [
            Department(id="scamt", name_en="SCAMT Institute"),
            Department(id="chem", name_en="Chemistry AI Unit", context_aliases=["Center for AI in Chemistry"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [
                    self._match_with(
                        root, departments, "Center for AI in Chemistry, SCAMT Institute, ITMO University"
                    ),
                    self._match_with(
                        root, departments, "Center for AI in Chemistry, Ioffe Institute, ITMO University"
                    ),
                ],
                [["scamt", "chem"], []],
            )

    def test_cyrillic_context_alias_two_parts_away_needs_a_sub_unit_in_between(self):
        department = Department(id="d1", name_en="Physics Unit", context_aliases=["Физический факультет"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [
                    self._match_with(root, [department], "Физический факультет, СПбГУ; Университет ИТМО"),
                    self._match_with(root, [department], "Физический факультет, кафедра оптики, Университет ИТМО"),
                ],
                [[], ["d1"]],
            )

    def test_context_alias_three_parts_away_is_rejected(self):
        affiliation = "Department of Physics, Laboratory of Optics, Group of Lasers, ITMO University"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match(Path(tmp), affiliation), [])

    def test_another_organisation_in_between_blocks_the_context(self):
        cases = [
            "Department of Physics, Laboratory of Optics of the Russian Academy of Sciences, ITMO University",
            "Department of Physics, Laboratory of Optics at Imperial College, ITMO University",
            "Department of Physics, Laboratory of Optics at Almazov Hospital, ITMO University",
            "Department of Physics, Лаборатория оптики Санкт-Петербургского университета, ITMO University",
            "ITMO University, Laboratory of Optics of Harvard University, Department of Physics",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual([self._match(root, case) for case in cases], [[]] * len(cases))

    def test_academic_is_not_another_academy(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._match(Path(tmp), "Department of Physics Academic Building, ITMO University"), ["d1"]
            )

    def test_long_russian_itmo_name_split_by_a_comma_is_a_marker(self):
        department = Department(id="d1", name_en="Math Unit", context_aliases=["Кафедра высшей математики"])
        affiliation = (
            "Кафедра высшей математики, Санкт-Петербургский национальный исследовательский университет "
            "информационных технологий, механики и оптики"
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match_with(Path(tmp), [department], affiliation), ["d1"])

    def test_context_alias_belongs_to_the_organisation_of_its_own_affiliation(self):
        department = Department(
            id="d1", name_en="Physics Unit", context_aliases=["Department of Physics", "Faculty of Physics"]
        )
        rejected = [
            "ITMO University, Russia; Department of Physics, Universidad de Chile, Santiago, Chile",
            "ITMO University, St. Petersburg 197101,; The Department of Physics, Saint Petersburg State University of "
            "Industrial Technologies and Design, St. Petersburg",
            "Department of Physics and Astronomy, University of Exeter, Exeter, UK; ITMO University, Saint Petersburg",
            "Lomonosov Moscow State University, Faculty of Physics; ITMO University",
            "Университет ИТМО, Россия; Физический факультет, Санкт-Петербургский государственный университет",
        ]
        accepted = [
            "Saint Petersburg State University; Faculty of Physics, ITMO University",
            "ITMO University, Department of Physics; Harvard University",
            "Faculty of Physics, ITMO University, St. Petersburg, Russia and Department of Physics, "
            "St. Petersburg State University",
            "Laboratory of Optics MIPT, Master's Student Faculty of Physics; ITMO University",
        ]
        department.context_aliases.append("Физический факультет")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [self._match_with(root, [department], case) for case in rejected + accepted],
                [[]] * len(rejected) + [["d1"]] * len(accepted),
            )

    def test_abbreviated_and_foreign_language_universities_are_other_organisations(self):
        department = Department(
            id="d1", name_en="Physics Unit", context_aliases=["Department of Physics", "Физический факультет"]
        )
        cases = [
            "Физический факультет СПбГУ; Университет ИТМО",
            "Физический факультет МГУ им. М.В. Ломоносова; Университет ИТМО, Санкт-Петербург",
            "Department of Physics SPbU; ITMO University",
            "Department of Physics Univ. Helsinki; ITMO University",
            "Department of Physics Universidade de Lisboa, ITMO University",
            "Department of Physics, MIPT; ITMO University",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual([self._match_with(root, [department], case) for case in cases], [[]] * len(cases))

    def test_itmo_marker_glued_to_a_word_or_digit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [
                    self._match(root, "Department of Physics, ITMOUniversity"),
                    self._match(root, "Department of Physics, 1ITMO University"),
                    self._match(root, "Department of Physics, Kitmo Labs"),
                    self._match(root, "Department of Physics, KitmoUniversity"),
                    self._match(root, "Department of Physics, Itmosphere Labs"),
                ],
                [["d1"], ["d1"], [], [], []],
            )

    def test_long_russian_itmo_name_without_a_comma_is_a_marker(self):
        department = Department(id="d1", name_en="Math Unit", context_aliases=["Кафедра высшей математики"])
        affiliation = "Кафедра высшей математики, Университет информационных технологий механики и оптики"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._match_with(Path(tmp), [department], affiliation), ["d1"])

    def test_marker_in_another_segment_needs_the_rest_of_the_segment_to_bridge(self):
        rejected = [
            "ITMO University, Russia; Department of Physics, KTH Royal Institute of Technology, Stockholm",
            "ITMO University, St. Petersburg; Department of Physics, ETH Zurich",
            "ITMO University; Laboratory of Quantum Optics, Department of Physics, Ioffe Institute",
        ]
        accepted = [
            "ITMO University; Department of Physics",
            "ITMO University; Department of Physics, Saint Petersburg, Russia",
            "ITMO University; Laboratory of Quantum Optics, Department of Physics",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [self._match(root, case) for case in rejected + accepted],
                [[]] * len(rejected) + [["d1"]] * len(accepted),
            )

    def test_address_and_title_parts_do_not_count_towards_the_distance(self):
        cases = [
            "ITMO University, Russia, Saint Petersburg, Kronverkskiy Ave., 49, 197101; "
            "Candidate of Technical Sciences, Associate Professor of Department of Physics",
            "Department of Physics, Kronverksky pr. 49, 197101, ITMO University",
            "Department of Physics, Кронверкский пр. 49, Университет ИТМО",
            "Department of Physics, Engineer, Senior Lecturer, ITMO University",
            "ITMO University, Saint Petersburg, Russia; PhD candidate, Department of Physics",
            "ITMO University, Kronverksky Prospect 49, Российская Федерация; Department of Physics",
            "Department of Physics, к.т.н., доцент, Университет ИТМО",
            "Department of Physics, (ITMO University), Saint Petersburg",
            "( Russian Federation , Saint Petersburg ) Associate Professor , Cand. Sci. (Educ.) , "
            "Department of Physics; ITMO University",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual([self._match(root, case) for case in cases], [["d1"]] * len(cases))

    def test_a_part_naming_an_organisation_is_never_skipped(self):
        cases = [
            "Department of Physics, (Moscow State University); ITMO University",
            "Department of Physics, Professor (MSU); ITMO University",
            "Department of Physics, Senior Researcher (Ioffe Institute); ITMO University",
            "Department of Physics, Candidate of Sciences at Institute of Applied Astronomy of the Russian Academy of "
            "Sciences, ITMO University",
            "Department of Physics, Doctor of Science Tokyo Institute of Science, ITMO University",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual([self._match(root, case) for case in cases], [[]] * len(cases))

    def test_title_words_inside_a_name_are_kept(self):
        department = Department(id="d1", name_en="Cryo Unit", context_aliases=["Engineering Department"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [
                    self._match_with(root, [department], "Engineering Department, ITMO University"),
                    self._match_with(
                        root, [department], "Engineering Department, Harvard University, ITMO University"
                    ),
                ],
                [["d1"], []],
            )

    def test_catalogue_name_bridges_only_when_it_opens_the_part(self):
        departments = [
            Department(id="bio", name_en="Bioengineering Institute"),
            Department(id="phys", name_en="Physics Unit", context_aliases=["Department of Physics"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                [
                    self._match_with(
                        root, departments, "Department of Physics, Bioengineering Institute, ITMO University"
                    ),
                    self._match_with(
                        root, departments, "Department of Physics, Auckland Bioengineering Institute, ITMO University"
                    ),
                ],
                [["bio", "phys"], ["bio"]],
            )

    def test_college_next_to_the_name_blocks_the_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._match(Path(tmp), "Department of Physics, Imperial College London; ITMO University"), []
            )

    def test_long_runs_of_spaces_and_repeated_words_stay_fast(self):
        affiliations = [
            " " * 50000 + "Department of Physics, ITMO University",
            "Department of Physics, " + "научно " * 60 + "общество, ITMO University",
            "Department of Physics, " + "candidate of " * 4000 + ", ITMO University",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual([self._match(root, affiliation) for affiliation in affiliations], [["d1"], [], []])


class CoverageLogTest(unittest.TestCase):
    """Coverage lines may only appear when the pass looked at every author."""

    LOGGER = "pauk.pipeline.stages.departments"

    def _stage(self, root: Path, *, selection=None, prepared=None):
        static = root / "static" / "itmo"
        static.mkdir(parents=True, exist_ok=True)
        (static / "departments.jsonl").write_text(
            Department(id="d1", name_en="Faculty of Physics").model_dump_json(), encoding="utf-8")
        db = mongomock.MongoClient()["pauk_test"]
        if prepared is None:
            persons = [
                Person(id=f"P{n}", is_member=True,
                       authored=[Authorship(publication_id="W1",
                                            affiliation="Faculty of Physics, ITMO University")])
                for n in (1, 2)
            ]
            prepared = _prepare(db, persons, [Publication(id="W1", title="t")])
        return prepared, DepartmentsStage(prepared, RawStore(db, "sample"),
                                          config=Settings(data_dir=root), selection=selection)

    def test_full_pass_reports_catalogue_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, stage = self._stage(Path(tmp))
            with self.assertLogs(self.LOGGER, level="INFO") as logs:
                stage.run()
            self.assertTrue(any("Matched 1 of 1 catalogue departments" in line for line in logs.output))
            self.assertTrue(any("Matched 2 of 2 member authors to a department" in line for line in logs.output))

    def test_author_coverage_counts_only_itmo_persons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = mongomock.MongoClient()["pauk_test"]
            persons = [
                Person(
                    id="P1",
                    is_member=True,
                    authored=[Authorship(publication_id="W1", affiliation="Faculty of Physics, ITMO University")],
                ),
                Person(
                    id="P2", is_member=True, authored=[Authorship(publication_id="W1", affiliation="ITMO University")]
                ),
                Person(
                    id="P3",
                    is_member=False,
                    authored=[Authorship(publication_id="W1", affiliation="Faculty of Physics, ITMO University")],
                ),
            ]
            prepared = _prepare(db, persons, [Publication(id="W1", title="t")])
            _, stage = self._stage(root, prepared=prepared)
            with self.assertLogs(self.LOGGER, level="INFO") as logs:
                stage.run()
            self.assertTrue(any("Matched 1 of 2 member authors to a department" in line for line in logs.output))

    def test_scoped_pass_stays_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, stage = self._stage(Path(tmp),
                                   selection=PreparedSelection("persons", frozenset({"P1"})))
            with self.assertNoLogs(self.LOGGER, level="INFO"):
                stage.run()

    def test_resumed_pass_stays_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared, stage = self._stage(root)
            stage.run()
            _, resumed = self._stage(root, prepared=prepared)
            with self.assertNoLogs(self.LOGGER, level="INFO"):
                resumed.run()


class CatalogueVariantsTest(unittest.TestCase):
    """Invariants of the committed catalogue."""

    CATALOGUE = Path(__file__).resolve().parents[2] / "data" / "static" / "itmo" / "departments_catalog.json"

    def setUp(self):
        self.entries = json.loads(self.CATALOGUE.read_text(encoding="utf-8-sig"))

    def test_no_variant_shorter_than_three_characters(self):
        short = [
            (e["uid"], v)
            for e in self.entries
            for v in [*e.get("aliases", []), *e.get("context_aliases", [])]
            if len(_normalize(v)) < 3
        ]
        self.assertEqual(short, [])

    def test_no_name_shared_by_two_units(self):
        owners: dict[str, set[str]] = {}
        for e in self.entries:
            for v in [e.get("name_en"), e.get("name_local"), *e.get("aliases", []), *e.get("context_aliases", [])]:
                if v:
                    owners.setdefault(_normalize(v), set()).add(e["uid"])
        self.assertEqual({v: u for v, u in owners.items() if len(u) > 1}, {})

    def test_no_name_inside_an_unrelated_unit_name(self):
        parent = {e["uid"]: e.get("parent") for e in self.entries}

        def lineage(uid):
            while uid:
                yield uid
                uid = parent.get(uid)

        names = {
            e["uid"]: {_normalize(v) for v in [e.get("name_en"), e.get("name_local"), *e.get("aliases", [])] if v}
            for e in self.entries
            if e.get("kind") != "organization"
        }
        clashes = [
            (outer, inner, name)
            for outer, outer_names in names.items()
            for inner, inner_names in names.items()
            if inner not in lineage(outer) and outer not in lineage(inner)
            for name in inner_names
            if any(_contains(name, other) for other in outer_names)
        ]
        self.assertEqual(clashes, [])

    def test_context_alias_has_no_part_separator(self):
        broken = [
            (e["uid"], v)
            for e in self.entries
            for v in e.get("context_aliases", [])
            if any(sep in v for sep in ",;\n")
        ]
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
