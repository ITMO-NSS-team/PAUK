"""Independent regressions for stored-source reassessment and graph publication."""

import copy
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import mongomock
from bson import json_util

from pauk.graph.client import _person_membership_update
from pauk.graph.extract import extract_node, extract_relationships, person_spec
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import merge_nodes
from pauk.graph.unmerge import NothingToRebuild, split_person
from pauk.models import Authorship, Person, Publication
from pauk.models.person import MembershipWorkSource
from pauk.pipeline.membership import ITMO_ROR_ID, apply_membership
from pauk.pipeline.membership_rebuild import rebuild_from_global_sources
from pauk.pipeline.normalize import _merge_person
from pauk.pipeline.stages.persons import PersonsStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from pauk.storage.person_merge_sources import preserve
from scripts.reassess_membership import apply_plan, write_plan
from tests.unit.test_admin_nodes import FakePanelGraph


class MembershipFinalQATest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().membership_final_qa
        self.before = datetime(2026, 10, 1, tzinfo=UTC)
        self.after = self.before + timedelta(days=1)

    def person(self, *, merged=False):
        owners = ("A1", "A2") if merged else ("A1",)
        person = Person(
            id="A1", merged_ids=list(owners[1:]), is_itmo=True,
            emails=["first@example.test", "second@example.test"],
            other_names=["Original spelling"],
            authored=[Authorship(
                publication_id="W1", position=index, is_corresponding=True,
                affiliation=f"Original text {owner}", institution_rors=[ITMO_ROR_ID],
                institution_source_person_id=owner, institution_source_id="W1",
                institution_observed_at=self.before,
            ) for index, owner in enumerate(owners, 1)],
        )
        apply_membership(person)
        if merged:
            for index, owner in enumerate(owners, 1):
                original = Person(id=owner, is_itmo=True, authored=[person.authored[index - 1]])
                self.db.person_merge_sources.insert_one({
                    "person_id": owner, "saved_at": self.before.isoformat(),
                    "snapshot": original.model_dump(mode="json"),
                })
        return person

    def source(self, institutions, *, truncated=False):
        self.db.raw.insert_one({
            "source": "openalex_works", "group": "other_run",
            "fetched_at": self.after.isoformat(),
            "payload": {"id": "W1", "is_authors_truncated": truncated,
                        "authorships": [{"author": {"id": owner}, "institutions": rors}
                                        for owner, rors in institutions]},
        })

    def test_reassessment_clears_published_authorship_evidence_too(self):
        person = self.person()
        self.source([("A1", [])])
        result = rebuild_from_global_sources(self.db, person)
        self.assertEqual((result.membership_status, result.is_itmo), ("unsupported", False))
        row = result.model_dump(mode="json")
        edges = extract_relationships(row, person_spec(row))
        props = edges[("Person", "Publication", "AUTHORED", "id")][0][2]
        self.assertEqual(props["institution_rors"], [])
        self.assertTrue(props["is_corresponding"])
        self.assertEqual(props["affiliation"], "Original text A1")
        self.assertEqual(result.emails, person.emails)
        self.assertEqual(result.other_names, person.other_names)
        self.assertEqual(person.authored[0].institution_rors, [ITMO_ROR_ID])

    def test_merged_reassessment_preserves_positive_original_owner(self):
        person = self.person(merged=True)
        self.source([("A1", []), ("A2", [{"ror": ITMO_ROR_ID}])])
        result = rebuild_from_global_sources(self.db, person)
        self.assertEqual((result.membership_status, result.is_itmo), ("supported", True))
        self.assertEqual({claim.source_person_id for claim in result.membership_evidence}, {"A2"})
        self.assertEqual({row.institution_source_person_id: row.institution_rors for row in result.authored},
                         {"A1": [], "A2": [ITMO_ROR_ID]})
        self.assertEqual(len(result.authored), 2)
        self.assertTrue(all(row.is_corresponding for row in result.authored))

    def test_complete_merged_withdrawal_clears_all_source_owners(self):
        person = self.person(merged=True)
        self.source([("A1", []), ("A2", [])])
        result = rebuild_from_global_sources(self.db, person)
        self.assertEqual((result.membership_status, result.is_itmo), ("unsupported", False))
        self.assertEqual(result.merged_ids, ["A2"])
        self.assertEqual({row.source_person_id for row in result.membership_owner_sources}, {"A1", "A2"})
        self.assertTrue(all(row.institution_rors == [] for row in result.authored))
        self.assertEqual(len(result.authored), 2)

    def test_truncated_withdrawal_cannot_erase_merged_source(self):
        person = self.person(merged=True)
        self.source([("A1", [])], truncated=True)
        original = copy.deepcopy(person.model_dump())
        result = rebuild_from_global_sources(self.db, person)
        self.assertTrue(result.is_itmo)
        self.assertEqual(result.authored, person.authored)
        self.assertEqual(result.membership_evidence, person.membership_evidence)
        self.assertEqual(person.model_dump(), original)

    def test_equal_time_conflict_does_not_clear_published_claim(self):
        person = self.person()
        self.after = self.before
        self.source([("A1", [])])
        result = rebuild_from_global_sources(self.db, person)
        self.assertTrue(result.is_itmo)
        self.assertEqual(result.authored[0].institution_rors, [ITMO_ROR_ID])

    def test_reordered_author_withdrawal_clears_old_display_claim(self):
        person = self.person()
        self.source([("A2", []), ("A1", [])])
        result = rebuild_from_global_sources(self.db, person)
        self.assertEqual((result.membership_status, result.is_itmo), ("unsupported", False))
        self.assertEqual(len(result.authored), 1)
        self.assertTrue(result.authored[0].is_corresponding)
        self.assertEqual(result.authored[0].institution_rors, [])
        self.assertEqual(result.authored[0].affiliation, "Original text A1")

    def test_unrelated_raw_group_is_used_without_mutation(self):
        person = self.person()
        self.source([("A1", [])])
        self.db.persons.insert_one(person.model_dump(mode="json") | {"groups": ["current_run"]})
        before = {name: list(self.db[name].find({})) for name in self.db.list_collection_names()}
        result = rebuild_from_global_sources(self.db, person)
        self.assertFalse(result.is_itmo)
        self.assertEqual({name: list(self.db[name].find({})) for name in before}, before)

    def test_real_stage_storage_roundtrip_keeps_one_orcid_observation_time(self):
        identifier = "0000-0002-1825-0097"
        stamp = self.after.replace(microsecond=123456)
        person = Person(
            id="A1", orcid=identifier, is_itmo=True,
            affiliations=[{"name": "ITMO", "source": "orcid", "ror": ITMO_ROR_ID,
                           "source_id": identifier, "source_person_id": "A1"}],
            membership_owner_sources=[{"source_person_id": "A1", "work_ids": [],
                                       "orcid_ids": [identifier], "observed_at": self.before}],
        )
        apply_membership(person)
        store = PreparedStore(self.db, "current_run")
        raw = RawStore(self.db, "current_run")
        store.write_models("persons", [person])
        payload = {"activities-summary": {"employments": {"affiliation-group": []}}}
        with tempfile.TemporaryDirectory() as directory, \
                patch("pauk.storage.raw.datetime") as raw_clock, \
                patch("pauk.pipeline.stages.persons.datetime") as stage_clock, \
                patch("pauk.pipeline.stages.persons.OrcidClient") as orcid, \
                patch("pauk.pipeline.stages.persons.OpenAlexClient"), \
                patch("pauk.pipeline.stages.persons.CrossrefClient"):
            raw_clock.now.return_value = stamp
            stage_clock.now.return_value = stamp + timedelta(milliseconds=1)
            orcid.return_value.get_record.return_value = payload
            PersonsStage(store, raw, Settings(data_dir=Path(directory)), force=True).run()
        stored = next(store.read_models("persons", Person))
        self.assertEqual((stored.membership_status, stored.is_itmo), ("unsupported", False))
        self.assertEqual(stored.membership_orcid_sources[0].observed_at, stamp)
        self.assertEqual(self.db.raw.find_one({"source": "orcid"})["fetched_at"], stamp.isoformat())
        reassessed = rebuild_from_global_sources(self.db, stored)
        self.assertEqual((reassessed.membership_status, reassessed.is_itmo), ("unsupported", False))

    def test_plan_and_apply_keep_metadata_when_legacy_display_gains_provenance(self):
        person = self.person()
        person.membership_work_sources = [MembershipWorkSource.model_validate(person.authored[0].model_dump())]
        person.authored[0].institution_source_person_id = None
        person.authored[0].institution_source_id = None
        original = person.model_dump(mode="json") | {"groups": ["first", "second"], "_version": 4}
        original["authored"][0]["provider_metadata"] = {"original": [1, None, "keep"]}
        self.db.persons.insert_one(original)
        self.source([("A1", [])])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan"
            report = write_plan(self.db, output)
            proposal = json_util.loads((output / "persons.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(proposal["authored"][0]["provider_metadata"],
                             original["authored"][0]["provider_metadata"])
            self.assertEqual(proposal["authored"][0]["institution_source_person_id"], "A1")
            apply_plan(self.db, output, report["plan_id"], apply=True)
            stored = self.db.persons.find_one({"id": "A1"})
            self.assertEqual(stored["authored"], proposal["authored"])
            self.assertEqual(stored["groups"], original["groups"])
            self.assertEqual(stored["_version"], 5)
            second = apply_plan(self.db, output, report["plan_id"], apply=True)
            self.assertEqual(second["already_applied"], 1)
            self.assertEqual(self.db.revisions.find_one({"entity_id": "A1"})["snapshot"], original)

    def test_reassessed_aggregate_publish_and_refused_split_preserve_both_stores(self):
        originals = []
        for position, owner in enumerate(("A1", "A2"), 1):
            person = Person(
                id=owner, is_itmo=False, emails=[f"{owner}@example.test"],
                authored=[Authorship(
                    publication_id="W1", position=position, is_corresponding=True,
                    affiliation=f"Original text {owner}",
                    institution_rors=[ITMO_ROR_ID] if owner == "A2" else [],
                    institution_source_person_id=owner, institution_source_id="W1",
                    institution_observed_at=self.before,
                )],
            )
            apply_membership(person)
            originals.append(person)
        store = PreparedStore(self.db, "current_run")
        store.write_models("persons", originals)
        store.write_models("publications", [Publication(id="W1", title="Original publication")])
        preserve(self.db, ("A1", "A2"))
        archives = list(self.db.person_merge_sources.find({}))
        graph = FakePanelGraph()

        def publish():
            load_prepared_rows(graph, {filename: list(store.read_rows(entity))
                                       for entity, filename in ENTITY_FILES.items()})

        publish()
        merge_nodes(graph, "Person", "A2", "A1")
        aggregate = _merge_person(originals[0].model_copy(deep=True), originals[1])
        store.write_models("persons", [aggregate])
        publish()
        stored_node = copy.deepcopy(graph.nodes[("Person", "A1")])
        self.assertTrue(stored_node["is_itmo"])
        self.source([("A1", []), ("A2", [])])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan"
            report = write_plan(self.db, output)
            apply_plan(self.db, output, report["plan_id"], apply=True)
        row = self.db.persons.find_one({"id": "A1"})
        _labels, (_identifier, props) = extract_node(row, person_spec(row))
        updated = _person_membership_update(stored_node, props, "A1")
        self.assertEqual((updated["membership_status"], updated["is_itmo"]), ("unsupported", False))
        publish()
        self.assertFalse(graph.nodes[("Person", "A1")]["is_itmo"])
        self.assertEqual(graph.nodes[("Person", "A1")]["membership_status"], "unsupported")
        self.assertEqual(set(row["emails"]), {"A1@example.test", "A2@example.test"})
        self.assertEqual(list(self.db.person_merge_sources.find({})), archives)
        graph_before = copy.deepcopy((graph.nodes, graph.relationships))
        mongo_before = {name: list(self.db[name].find({})) for name in self.db.list_collection_names()}
        with self.assertRaises(NothingToRebuild):
            split_person(graph, self.db, ["A1", "A2"])
        self.assertEqual((graph.nodes, graph.relationships), graph_before)
        self.assertEqual({name: list(self.db[name].find({})) for name in mongo_before}, mongo_before)


if __name__ == "__main__":
    unittest.main()
