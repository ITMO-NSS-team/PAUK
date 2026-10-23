"""The UFJF profile end to end, and the profile switch itself.

Affiliation strings below are copied from OpenAlex works of UFJF authors, so
the department cases are how people actually write them.
"""

import importlib
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk import institution
from pauk.institution import ITMO, UFJF, Institution
from pauk.models import Person, Publication
from pauk.models.relations import Authorship
from pauk.pipeline.collect import Collector
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.selectors import PeriodSelector
from pauk.pipeline.stages.author_names import AuthorNamesStage, catalog_path, required_name_field_issues
from pauk.pipeline.stages.departments import DepartmentsStage
from pauk.pipeline.stages.emails import pick_email
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from pauk.storage.static import StaticStore

REPO_ROOT = Path(__file__).resolve().parents[2]
UFJF_STATIC = REPO_ROOT / "data" / "static" / "ufjf"


class ProfileTest(unittest.TestCase):
    def test_profiles_are_looked_up_by_key_in_any_case(self):
        self.assertIs(institution.get("UFJF"), UFJF)
        self.assertIs(institution.get(" itmo "), ITMO)
        with self.assertRaises(ValueError):
            institution.get("no-such-university")

    def test_membership_by_openalex_ror_url_or_bare_id(self):
        self.assertTrue(UFJF.is_member_ror("https://ror.org/04yqw9c44"))
        self.assertTrue(UFJF.is_member_ror("04yqw9c44"))
        self.assertFalse(UFJF.is_member_ror("https://ror.org/04txgxn49"))
        # The university hospital has its own ROR, which the profile leaves out.
        self.assertFalse(UFJF.is_member_ror("https://ror.org/0039g6563"))
        self.assertFalse(UFJF.is_member_ror(None))

    def test_institutional_email_includes_subdomains_but_not_lookalikes(self):
        for email in ("ana@ufjf.br", "ana@ice.ufjf.br", "ana@estudante.ufjf.br", "ana@ufjf.edu.br"):
            with self.subTest(email=email):
                self.assertTrue(UFJF.is_institutional_email(email))
        for email in ("ana@notufjf.br", "ana@gmail.com", "ana@itmo.ru"):
            with self.subTest(email=email):
                self.assertFalse(UFJF.is_institutional_email(email))
        self.assertTrue(ITMO.is_institutional_email("ivan@ifmo.ru"))

    def test_pick_email_prefers_the_profiles_domain(self):
        options = {"a@gmail.com", "ana.silva@ice.ufjf.br", "ivan@itmo.ru"}
        self.assertEqual(pick_email(options, UFJF), "ana.silva@ice.ufjf.br")
        self.assertEqual(pick_email(options, ITMO), "ivan@itmo.ru")


class ProfileFileTest(unittest.TestCase):
    """A new university is a profile.json, not Python."""

    def write(self, root, **overrides):
        data = {f.name: getattr(UFJF, f.name) for f in fields(Institution)}
        data.update(key="sample", short_name="SU", marker=r"\bsu\b", marker_replacement="SU", **overrides)
        data["email_domains"] = list(data["email_domains"])
        data["surname_particles"] = list(data["surname_particles"])
        path = root / "sample" / "profile.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_a_profile_is_read_from_its_static_folder(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(institution, "STATIC_ROOT", Path(tmp)):
            self.write(Path(tmp), email_domains=["su.edu"])
            profile = institution.get("Sample")
            self.assertEqual(profile.short_name, "SU")
            self.assertTrue(profile.is_institutional_email("ana@cs.su.edu"))

    def test_an_unknown_field_is_an_error_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), markr="typo")
            with self.assertRaisesRegex(ValueError, "markr"):
                institution.load(path)

    def test_an_unknown_key_names_the_known_ones(self):
        with self.assertRaisesRegex(ValueError, "itmo"):
            institution.get("no-such-university")


class UnitWordsTest(unittest.TestCase):
    """A university whose units are called colleges must not read them as other organisations."""

    CATALOG = [
        {"uid": "fudan", "name_en": "Fudan University", "kind": "organization", "parent": None},
        {"uid": "cs", "name_en": "School of Computer Science", "kind": "school", "parent": "fudan",
         "context_aliases": ["School of Computer Science", "College of Computer Science"]},
    ]

    def matched(self, profile, affiliation):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "departments_catalog.json").write_text(json.dumps(self.CATALOG), encoding="utf-8")
            departments = StaticStore(Path(tmp)).departments()
        from pauk.pipeline.stages.departments import _context_names, _match_names, _match_person
        return _match_person([affiliation], [(d.id, _match_names(d)) for d in departments],
                             [(d.id, _context_names(d)) for d in departments], profile)

    def test_college_is_a_unit_only_where_the_profile_says_so(self):
        base = dict(marker=r"(?<![^\W\d_])fudan(?![^\W\d_])", long_name="(?!)", marker_replacement="Fudan")
        plain = replace(UFJF, **base)
        with_units = replace(UFJF, **base, unit_words=r"\bcollege\s+of\b")
        text = "College of Computer Science, Fudan University, Shanghai"
        self.assertEqual(self.matched(plain, text), [])
        self.assertEqual(self.matched(with_units, text), ["cs"])


class SettingsTest(unittest.TestCase):
    def test_each_university_gets_its_own_database_and_folders(self):
        with patch.dict(os.environ, {"MONGO_DB": ""}):
            config = Settings(data_dir=Path("/data"), institution_key="ufjf", mongo_db="")
        self.assertEqual(config.mongo_db, "pauk_ufjf")
        self.assertEqual(config.static_dir, Path("/data/static/ufjf"))
        self.assertEqual(config.cache_dir, Path("/data/cache/ufjf"))
        self.assertEqual(config.gui_dir, Path("/data/gui/ufjf"))
        self.assertEqual(config.audit_dir, Path("/data/audit/ufjf"))
        # PDFs are keyed by work, so the universities share them.
        self.assertEqual(config.pdf_dir, Path("/data/pdf"))
        self.assertEqual(Settings(institution_key="itmo", mongo_db="").mongo_db, "pauk")

    def test_an_explicit_database_wins(self):
        self.assertEqual(Settings(institution_key="ufjf", mongo_db="pauk_test").mongo_db, "pauk_test")

    def test_ufjf_has_no_staff_catalog_unless_one_is_given(self):
        self.assertIsNone(catalog_path(Settings(institution_key="ufjf", staff_catalog_file=None)))
        self.assertEqual(catalog_path(Settings(institution_key="ufjf", staff_catalog_file="/x/staff.csv")),
                         Path("/x/staff.csv"))
        itmo = Settings(data_dir=Path("/data"), institution_key="itmo", staff_catalog_file=None)
        self.assertEqual(catalog_path(itmo), Path("/data/static/itmo/russian_names.csv"))


class CollectAndNormalizeTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.raw = RawStore(self.db, "sample")

    def test_a_period_is_collected_by_the_profiles_ror(self):
        asked = []

        class Client:
            def iter_works(self, ror_id, date_from, date_to):
                asked.append(ror_id)
                return iter(())

        Collector(Client(), self.raw, UFJF).collect(PeriodSelector("2025-01-01", "2025-12-31"))
        self.assertEqual(asked, ["04yqw9c44"])

    def test_only_an_authorship_at_the_university_makes_a_member(self):
        self.raw.append("openalex_works", {
            "id": "https://openalex.org/W1", "title": "Paper", "publication_date": "2025-01-02",
            "authorships": [
                {"author": {"id": "https://openalex.org/A1", "display_name": "Ana Silva"},
                 "institutions": [{"ror": "https://ror.org/04yqw9c44"}],
                 "raw_affiliation_strings": ["Universidade Federal de Juiz de Fora"]},
                {"author": {"id": "https://openalex.org/A2", "display_name": "Bruno Costa"},
                 "institutions": [{"ror": "https://ror.org/0039g6563"}],
                 "raw_affiliation_strings": ["Hospital Universitário, Juiz de Fora"]},
                {"author": {"id": "https://openalex.org/A3", "display_name": "Ivan Petrov"},
                 "institutions": [{"ror": "https://ror.org/04txgxn49"}]},
            ],
        }, {"work_id": "W1"})
        prepared = PreparedStore(self.db, "sample")
        OpenAlexNormalizer(self.raw, prepared, UFJF).run()
        members = {p.id: p.is_member for p in prepared.read_models("persons", Person)}
        self.assertEqual(members, {"A1": True, "A2": False, "A3": False})


class UfjfCatalogueTest(unittest.TestCase):
    def setUp(self):
        self.store = StaticStore(UFJF_STATIC)
        self.departments = self.store.departments()

    def test_the_root_is_ufjf_with_its_ror(self):
        [root] = self.store.organizations()
        self.assertEqual(root.id, "ufjf")
        self.assertTrue(UFJF.is_member_ror(root.ror_id))

    def test_every_unit_has_both_names_and_hangs_off_the_tree(self):
        ids = {d.id for d in self.departments}
        for department in self.departments:
            with self.subTest(department=department.id):
                self.assertTrue(department.name_local)
                self.assertTrue(department.name_en)
                self.assertTrue(department.organization_id == "ufjf" or department.parent_id in ids)

    def test_no_context_alias_holds_a_part_separator(self):
        # Affiliations are cut into parts on commas and semicolons before
        # context aliases are looked for, so such an alias could never match.
        for department in self.departments:
            for alias in department.context_aliases:
                with self.subTest(alias=alias):
                    self.assertNotRegex(alias, "[,;]")

    def test_generic_official_names_are_matched_only_next_to_ufjf(self):
        chemistry = next(d for d in self.departments if d.id == "ice-qui")
        self.assertIn(chemistry.name_local, chemistry.context_aliases)
        self.assertIn(chemistry.name_en, chemistry.context_aliases)

    def test_github_organizations_load(self):
        self.assertIn("peteletricaufjf", self.store.github_orgs)


class UfjfDepartmentsStageTest(unittest.TestCase):
    """The stage with the real UFJF catalogue, on affiliations as authors write them."""

    MATCHES = [
        ("Departamento de Engenharia Elétrica, Universidade Federal de Juiz de Fora (UFJF), Juiz de Fora, Brazil",
         {"eng-ppee"}),
        ("Department of Computer Science, Federal University of Juiz de Fora, Juiz de Fora, Brazil", {"ice-dcc"}),
        ("Departamento de Física—Instituto de Ciências Exatas, Universidade Federal de Juiz de Fora, 33036-900, "
         "Juiz de Fora, MG, Brazil", {"ice", "ice-fis"}),
        ("Department of Dental Clinic, School of Dentistry, Federal University of Juiz de Fora, Juiz de Fora, MG Brazil",
         {"odo-clo", "odontologia"}),
        ("Post Graduate Program in Health, Department of Internal Medicine, Medicine School, Federal University of "
         "Juiz de Fora, Juiz de Fora 36038-330, Brazil", {"medicina", "med-cli"}),
        ("Department of Anesthesiology , Juiz de Fora Federal University , Juiz de Fora , Brazil; Faculty of Medicine, "
         "Juiz de Fora Federal University", {"medicina"}),
        ("Universidade Federal de Juiz de Fora - campus avançado Governador Valadares", {"campus-gv"}),
        ("Department of Pharmacy, Federal University of Juiz de Fora, Governador Valadares, Brazil", {"campus-gv"}),
        ("Programa de Pós-graduação Em Ciências Aplicadas à Saúde - PPgCAS, Universidade Federal de Juiz de Fora - "
         "UFJF, Governador Valadares, MG, 35010-180, Brazil", {"icv", "campus-gv"}),
        ("Universidade federal de Juíz de Fora-UFJF; Department of Parasitology, Microbiology and Immunology, "
         "Federal University of Juiz de Fora, Juiz de Fora", {"icb-pmi"}),
        ("Laboratory of Plankton Ecology, Department of Zoology, Institute of Biology, Federal University of Juiz "
         "de Fora, Juiz de Fora, 36036-900, Brazil", {"icb", "icb-zoo"}),
        ("Graduate Program in Computational Modeling, Federal University of Juiz de Fora", {"pgmc"}),
        ("Universidade Federal de Juiz de Fora/ Colégio de Aplicação João XXIII", {"joao-xxiii"}),
    ]

    NO_MATCH = [
        # The same department name at a neighbouring university.
        "Departamento de Química, Universidade Federal de Minas Gerais, Belo Horizonte, Brazil",
        "Department of Food Technology, Federal University of Viçosa, Viçosa, MG, Brazil",
        # A private medical school in the same city.
        "Faculdade de Ciências Médicas e da Saúde de Juiz de Fora – SUPREMA, Juiz de Fora, Brazil",
        "Hospital de Pronto Socorro João XXIII Belo Horizonte",
        # Nothing but the university and its address.
        "Universidade Federal de Juiz de Fora, Rua José Lourenço Kelmer, s/n, Campus Universitário, São Pedro, "
        "Juiz de Fora, MG, 36036-900, Brasil",
    ]

    def run_stage(self, affiliations):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Settings(data_dir=Path(tmp.name), institution_key="ufjf")
        shutil.copytree(UFJF_STATIC, config.static_dir)
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        people = [
            Person(id=f"P{i}", is_member=True,
                   authored=[Authorship(publication_id=f"W{i}", affiliation=affiliation)])
            for i, affiliation in enumerate(affiliations)
        ]
        prepared.write_models("persons", people)
        prepared.write_models("publications", [Publication(id=f"W{i}", title="t") for i in range(len(people))])
        DepartmentsStage(prepared, RawStore(db, "sample"), config=config).run()
        found = {p.id: set(p.department_ids) for p in prepared.read_models("persons", Person)}
        return [found[f"P{i}"] for i in range(len(affiliations))]

    def test_affiliations_naming_a_unit_are_matched(self):
        found = self.run_stage([affiliation for affiliation, _ in self.MATCHES])
        for (affiliation, expected), got in zip(self.MATCHES, found, strict=True):
            with self.subTest(affiliation=affiliation):
                self.assertEqual(got, expected)

    def test_other_organisations_and_bare_addresses_are_not(self):
        for affiliation, got in zip(self.NO_MATCH, self.run_stage(self.NO_MATCH), strict=True):
            with self.subTest(affiliation=affiliation):
                self.assertEqual(got, set())


class _FakeClient:
    replies: list = []

    def __init__(self, *args, **kwargs):
        self.last_response = self.last_usage = self.last_error = None
        self.prompts = []
        self._replies = list(self.replies)

    def chat_json(self, prompt):
        self.prompts.append(prompt)
        reply = self._replies.pop(0) if self._replies else None
        self.last_error = None if reply else "simulated failure"
        return reply


def _split(surname, first, second=None):
    return {"matched_candidate": None, "surname_local": surname, "first_name_local": first,
            "second_name_local": second, "surname_en": surname, "first_name_en": first,
            "second_name_en": second, "reason": ""}


class UfjfAuthorNamesTest(unittest.TestCase):
    def run_stage(self, people, replies):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # No staff catalog anywhere: UFJF has none, and the stage must not ask for it.
        config = Settings(data_dir=Path(tmp.name), institution_key="ufjf", staff_catalog_file=None,
                          author_names_concurrency=1)
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        prepared.write_models("persons", people)
        client = type("_Client", (_FakeClient,), {"replies": replies})
        with patch("pauk.pipeline.stages.author_names.OpenRouterClient", client):
            AuthorNamesStage(prepared, RawStore(db, "sample"), config).run()
        return {p.id: p for p in prepared.read_models("persons", Person)}

    def test_a_portuguese_split_completes_without_a_staff_catalog(self):
        people = self.run_stage(
            [Person(id="A1", is_member=True, name_raw="Felipe De Paula Alves Benedicto")],
            [_split("Benedicto", "Felipe de Paula Alves")],
        )
        row = people["A1"]
        self.assertEqual(row.processing["author_names"].status, "completed")
        self.assertEqual(row.name_local, "Benedicto Felipe de Paula Alves")
        self.assertIsNone(row.second_name_local)

    def test_matched_candidate_means_nothing_without_candidates(self):
        # Seen on real UFJF names: with no staff catalog the model still filled
        # the field, with true or the person's own name, and every such reply
        # cost a retry.
        for junk in (True, "Michelle Abdo Paiva", 0):
            with self.subTest(matched_candidate=junk):
                people = self.run_stage(
                    [Person(id="A1", is_member=True, name_raw="Michelle Abdo Paiva")],
                    [{**_split("Paiva", "Michelle Abdo"), "matched_candidate": junk}],
                )
                self.assertEqual(people["A1"].processing["author_names"].status, "completed")
                self.assertEqual(people["A1"].processing["author_names"].attempts, 1)

    def test_a_leading_particle_goes_back_to_the_given_names(self):
        people = self.run_stage(
            [Person(id="A1", is_member=True, name_raw="Luiz Fernando Cappa de Oliveira"),
             Person(id="A2", is_member=True, name_raw="Thiago Pereira Da Silva"),
             Person(id="A3", is_member=True, name_raw="Mariana Leite Simões e Silva")],
            [_split("de Oliveira", "Luiz Fernando Cappa"), _split("Da Silva", "Thiago Pereira"),
             _split("Simões e Silva", "Mariana Leite")],
        )
        self.assertEqual((people["A1"].surname_local, people["A1"].first_name_local),
                         ("Oliveira", "Luiz Fernando Cappa de"))
        self.assertEqual((people["A2"].surname_en, people["A2"].first_name_en), ("Silva", "Thiago Pereira Da"))
        # "e" inside a compound surname is not a leading particle.
        self.assertEqual(people["A3"].surname_local, "Simões e Silva")

    def test_a_middle_name_never_lands_in_the_patronymic_slot(self):
        # The Russian guard keeps an initial there; UFJF has no patronymics at all.
        people = self.run_stage(
            [Person(id="A1", is_member=True, name_raw="Marcio E. C. Rodrigues")],
            [_split("Rodrigues", "Marcio", "E C")],
        )
        row = people["A1"]
        self.assertEqual(row.first_name_local, "Marcio E C")
        self.assertIsNone(row.second_name_local)
        self.assertEqual(row.name_local, "Rodrigues Marcio E C")

    def test_a_cyrillic_local_name_is_rejected_and_the_failure_keeps_the_raw_name(self):
        people = self.run_stage(
            [Person(id="A1", is_member=True, name_raw="Ana Silva")],
            [_split("Сильва", "Ана"), _split("Сильва", "Ана")],
        )
        row = people["A1"]
        self.assertEqual(row.processing["author_names"].status, "failed")
        self.assertIsNone(row.surname_local)
        # Not to_cyrillic: the local language is written in Latin script.
        self.assertEqual(row.name_local, "Ana Silva")

    def test_the_prompt_asks_for_portuguese_and_names_ufjf(self):
        prompts = []

        class Recording(_FakeClient):
            replies = [_split("Silva", "Ana")]

            def chat_json(self, prompt):
                prompts.append(prompt)
                return super().chat_json(prompt)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Settings(data_dir=Path(tmp.name), institution_key="ufjf", staff_catalog_file=None,
                          author_names_concurrency=1)
        db = mongomock.MongoClient()["pauk_test"]
        prepared = PreparedStore(db, "sample")
        prepared.write_models("persons", [Person(id="A1", is_member=True, name_raw="Ana Silva")])
        with patch("pauk.pipeline.stages.author_names.OpenRouterClient", Recording):
            AuthorNamesStage(prepared, RawStore(db, "sample"), config).run()
        [prompt] = prompts
        self.assertIn("UFJF", prompt)
        self.assertIn("Brazilian Portuguese", prompt)
        self.assertNotIn("Cyrillic", prompt)
        self.assertNotIn("ITMO", prompt)
        self.assertNotIn("matched_candidate", prompt)

    def test_the_completed_contract_follows_the_local_script(self):
        latin = {"surname_local": "Silva", "first_name_local": "Ana", "surname_en": "Silva", "first_name_en": "Ana"}
        self.assertEqual(required_name_field_issues(latin, "latin"), {})
        self.assertIn("surname_local", required_name_field_issues(latin, "cyrillic"))


class AdminChecksTest(unittest.TestCase):
    """The Russian-name checks drop out of the panel for a Latin-script university."""

    def tearDown(self):
        import pauk.admin.checks
        import pauk.settings

        importlib.reload(pauk.settings)
        importlib.reload(pauk.admin.checks)

    def checks_for(self, key):
        import pauk.admin.checks
        import pauk.settings

        with patch.dict(os.environ, {"PAUK_INSTITUTION": key}):
            importlib.reload(pauk.settings)
            return {c.id for c in importlib.reload(pauk.admin.checks).CHECKS}

    def test_patronymic_and_transliteration_checks_are_itmo_only(self):
        russian = {"itmo_no_patronymic", "name_mixed_script", "name_latin_only"}
        self.assertLessEqual(russian, self.checks_for("itmo"))
        ufjf = self.checks_for("ufjf")
        self.assertFalse(russian & ufjf)
        self.assertIn("itmo_no_dept", ufjf)


class WebInstitutionNamesTest(unittest.TestCase):
    def test_the_site_knows_every_profile(self):
        # src/core/i18n.ts mirrors the profiles; a profile it does not know
        # would build a site that names ITMO.
        source = (REPO_ROOT / "pauk" / "gui" / "web" / "src" / "core" / "i18n.ts").read_text(encoding="utf-8")
        for key, profile in institution.PROFILES.items():
            with self.subTest(institution=key):
                self.assertIn(f"{key}: ", source)
                self.assertIn(f'en: "{profile.short_name}"', source)


if __name__ == "__main__":
    unittest.main()


class PublishWithoutExternalsTest(unittest.TestCase):
    def published_people(self, key):
        db = mongomock.MongoClient()["pauk_test"]
        PreparedStore(db, "g").write_models("persons", [
            Person(id="A1", is_member=True, authored=[Authorship(publication_id="W1")]),
            Person(id="A2", is_member=False, authored=[Authorship(publication_id="W1")]),
        ])
        loaded = {}
        with patch("pauk.graph.load.audited_client"), patch("pauk.graph.load.create_constraints"), \
                patch("pauk.graph.load.apply_overrides", return_value={}), \
                patch("pauk.graph.load.load_prepared_rows",
                      side_effect=lambda client, rows, *a, **k: loaded.update(rows)):
            from pauk.graph.load import _load_locked
            _load_locked(Settings(institution_key=key), db, "g")
        return [row["id"] for row in loaded["persons.jsonl"]]

    def test_external_coauthors_are_published_only_where_the_profile_keeps_them(self):
        self.assertEqual(self.published_people("ufjf"), ["A1"])
        self.assertEqual(sorted(self.published_people("itmo")), ["A1", "A2"])
