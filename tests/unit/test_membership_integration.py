"""Cross-layer membership evidence and ownership regressions."""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.unmerge import split_person
from pauk.gui.graph_builder.authorship import build_authorship_index
from pauk.models import Authorship, Person, Publication
from pauk.pipeline.membership import POLICY_VERSION, apply_membership
from pauk.pipeline.normalize import ITMO_ROR_ID, OpenAlexNormalizer
from pauk.pipeline.stages.dedup import DedupStage
from pauk.pipeline.stages.persons import PersonsStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from tests.bench.mocks import RecordingNeo4jClient
from tests.unit.test_admin_nodes import FakePanelGraph


class MembershipPanelGraph(FakePanelGraph):
    """Mirror the client's AUTHORED union and provenance rules for this fixture."""

    def upsert_relationships_batch(self, src_label, tgt_label, rel_type, relationships, tgt_match_prop="id"):
        relationships = list(relationships)
        if rel_type != "AUTHORED":
            return super().upsert_relationships_batch(src_label, tgt_label, rel_type, relationships, tgt_match_prop)
        batch = {}
        counts = {}
        for source, target, props in relationships:
            key = (source, target)
            counts[key] = counts.get(key, 0) + 1
            if key not in batch:
                batch[key] = dict(props)
                continue
            previous = batch[key]
            owners = {name: previous.get(name) if previous.get(name) == props.get(name) else None
                      for name in ("institution_source_person_id", "institution_source_id")}
            rors = self.union_rors(previous.get("institution_rors"), props.get("institution_rors"))
            previous.update(props)
            previous.update(owners, institution_rors=rors)
        matched = 0
        for (source, target), props in batch.items():
            key = (src_label, rel_type, tgt_label, source, target)
            before = dict(self.relationships.get(key, {}))
            preserve = bool(self.nodes.get((src_label, source), {}).get("merged_ids")
                            or self.nodes.get((tgt_label, target), {}).get("merged_ids"))
            matched += counts[(source, target)] * super().upsert_relationships_batch(
                src_label, tgt_label, rel_type, [(source, target, props)], tgt_match_prop,
            )
            edge = self.relationships.get(key)
            if not preserve or edge is None:
                continue
            edge["institution_rors"] = self.union_rors(before.get("institution_rors"),
                                                       edge.get("institution_rors"))
            for name in ("institution_source_person_id", "institution_source_id"):
                had_source = before.get("institution_rors") is not None or before.get(name) is not None
                if had_source and (before.get(name) is None or edge.get(name) is None
                                   or before.get(name) != edge.get(name)):
                    edge[name] = None
        return matched

    @staticmethod
    def union_rors(first, second):
        if first is None and second is None:
            return None
        return list(dict.fromkeys([*(first or []), *(second or [])]))


class MembershipIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["membership_integration"]
        self.prepared = PreparedStore(self.db, "first")
        self.raw = RawStore(self.db, "first")
        self.fetch_sequence = 0
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Settings(data_dir=Path(directory.name), person_resolution_enabled=False)

    def work(self, work_id="W1", person_id="A1", institutions=None, *, omit=False):
        authorship = {"author": {"id": person_id, "display_name": "Alex Sample"}}
        if not omit:
            authorship["institutions"] = institutions if institutions is not None else []
        return {"id": work_id, "publication_date": "2018-01-01", "authorships": [authorship]}

    def normalize(self, work, group="first", observed_at=None):
        raw = RawStore(self.db, group)
        self.fetch_sequence += 1
        observed_at = observed_at or datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=self.fetch_sequence)
        with patch("pauk.storage.raw.datetime") as clock:
            clock.now.return_value = observed_at
            raw.append("openalex_works", work, {})
        prepared = PreparedStore(self.db, group)
        OpenAlexNormalizer(raw, prepared).run()
        return next(prepared.read_models("persons", Person))

    def profile_stage(self, *, author=None, orcid=None, error=None):
        with patch("pauk.pipeline.stages.persons.OpenAlexClient") as oa, \
                patch("pauk.pipeline.stages.persons.OrcidClient") as oc, \
                patch("pauk.pipeline.stages.persons.CrossrefClient"):
            oa.return_value.get_author.return_value = author or {}
            oc.return_value.get_record.return_value = orcid or {}
            if error:
                oc.return_value.get_record.side_effect = error
            PersonsStage(self.prepared, self.raw, self.config, force=True).run()
        return next(self.prepared.read_models("persons", Person))

    def test_raw_target_ror_without_affiliation_text_is_confirmed(self):
        person = self.normalize(self.work(institutions=[{"ror": f"https://ror.org/{ITMO_ROR_ID}"}]))
        self.assertTrue(person.is_itmo)
        self.assertEqual(person.authored[0].institution_rors, [ITMO_ROR_ID])
        self.assertEqual(person.authored[0].institution_source_person_id, "A1")
        self.assertIsNone(person.authored[0].affiliation)

    def test_missing_institutions_remains_unknown_not_known_empty(self):
        person = self.normalize(self.work(omit=True))
        self.assertIsNone(person.authored[0].institution_rors)
        self.assertEqual(person.membership_status, "unknown")

    def test_partial_new_group_does_not_erase_other_work_evidence(self):
        self.normalize(self.work(institutions=[{"ror": ITMO_ROR_ID}]))
        person = self.normalize(self.work("W2", institutions=[{"ror": "different"}]), "second")
        self.assertTrue(person.is_itmo)
        self.assertEqual({a.publication_id for a in person.authored}, {"W1", "W2"})
        document = self.db.persons.find_one({"id": "A1"})
        self.assertEqual(set(document["groups"]), {"first", "second"})

    def test_explicit_authorship_is_not_broadcast_to_every_coauthor(self):
        work = self.work(institutions=[{"ror": ITMO_ROR_ID}])
        work["authorships"].append({"author": {"id": "A2", "display_name": "Other Author"},
                                    "institutions": []})
        self.normalize(work)
        people = {p.id: p for p in self.prepared.read_models("persons", Person)}
        self.assertTrue(people["A1"].is_itmo)
        self.assertFalse(people["A2"].is_itmo)
        self.assertEqual(people["A2"].membership_evidence, [])

    def test_openalex_profile_target_does_not_promote_known_external(self):
        self.normalize(self.work(institutions=[]))
        result = self.profile_stage(author={"affiliations": [{
            "institution": {"display_name": "ITMO University", "ror": f"https://ror.org/{ITMO_ROR_ID}"},
            "years": [2018],
        }]})
        self.assertFalse(result.is_itmo)
        self.assertEqual(result.membership_evidence, [])

    def test_legacy_true_without_evidence_is_preserved_as_unknown(self):
        self.prepared.write_models("persons", [Person(id="A1", openalex_id="A1", is_itmo=True)])
        result = self.profile_stage()
        self.assertTrue(result.is_itmo)
        self.assertEqual(result.membership_status, "unknown")

    def test_failed_orcid_refresh_does_not_remove_former_employment(self):
        person = Person(id="A1", orcid="0000-0002-1825-0097", is_itmo=False,
                        affiliations=[{"name": "ITMO", "ror": ITMO_ROR_ID,
                                       "source": "orcid", "years": [2008], "source_person_id": "A1"}])
        apply_membership(person)
        before = person.membership_evidence
        self.prepared.write_models("persons", [person])
        result = self.profile_stage(error=RuntimeError("offline"))
        self.assertTrue(result.is_itmo)
        self.assertEqual(result.membership_evidence, before)
        self.assertEqual(result.processing["orcid"].status.value, "failed")

    def test_repeated_normalization_does_not_duplicate_membership_evidence(self):
        work = self.work(institutions=[{"ror": ITMO_ROR_ID}])
        first = self.normalize(work)
        OpenAlexNormalizer(self.raw, self.prepared).run()
        second = next(self.prepared.read_models("persons", Person))
        self.assertEqual(second.membership_evidence, first.membership_evidence)
        self.assertEqual(second.authored, first.authored)

    def test_explicit_same_source_correction_retracts_only_that_work_claim(self):
        self.normalize(self.work(institutions=[{"ror": ITMO_ROR_ID}]))
        result = self.normalize(self.work(institutions=[]))
        self.assertEqual(result.membership_evidence, [])
        self.assertFalse(result.is_itmo)
        self.assertEqual(result.membership_status, "unsupported")

    def test_correction_does_not_retract_a_different_work_claim(self):
        self.normalize(self.work(institutions=[{"ror": ITMO_ROR_ID}]))
        self.normalize(self.work("W2", institutions=[{"ror": ITMO_ROR_ID}]))
        result = self.normalize(self.work(institutions=[]))
        self.assertTrue(result.is_itmo)
        self.assertEqual({e.publication_id for e in result.membership_evidence}, {"W2"})

    def test_reordered_author_correction_does_not_keep_old_positive_slot(self):
        self.normalize(self.work(institutions=[{"ror": ITMO_ROR_ID}]))
        corrected = self.work(institutions=[])
        corrected["authorships"].insert(0, {
            "author": {"id": "A2", "display_name": "Other Author"}, "institutions": [],
        })
        self.normalize(corrected)
        person = next(p for p in self.prepared.read_models("persons", Person) if p.id == "A1")
        self.assertEqual(person.membership_evidence, [])
        self.assertFalse(person.is_itmo)
        self.assertEqual(person.membership_status, "unsupported")

    def test_author_removal_retracts_its_claim_but_keeps_the_person_and_other_work(self):
        self.normalize(self.work(institutions=[{"ror": ITMO_ROR_ID}]))
        self.normalize(self.work("W2", institutions=[{"ror": ITMO_ROR_ID}]))
        corrected = self.work(person_id="A2", institutions=[{"ror": ITMO_ROR_ID}])
        corrected["is_authors_truncated"] = False
        self.normalize(corrected, "second")
        people = {p.id: p for p in self.prepared.get_models("persons", ["A1", "A2"], Person)}
        self.assertIn("A1", people)
        self.assertTrue(people["A1"].is_itmo)
        self.assertEqual({e.publication_id for e in people["A1"].membership_evidence}, {"W2"})
        self.assertEqual({e.source_person_id for e in people["A2"].membership_evidence}, {"A2"})

    def test_truncated_author_list_does_not_retract_absent_author_claim(self):
        self.normalize(self.work(institutions=[{"ror": ITMO_ROR_ID}]))
        partial = self.work(person_id="A2", institutions=[])
        partial["is_authors_truncated"] = True
        self.normalize(partial, "second")
        person = next(self.prepared.get_models("persons", ["A1"], Person))
        self.assertTrue(person.is_itmo)
        self.assertEqual({e.publication_id for e in person.membership_evidence}, {"W1"})

    def test_correction_keeps_another_merged_owner_claim_on_the_same_work(self):
        self.prepared.write_models("persons", [Person(
            id="A1", openalex_id="A1", is_itmo=False, merged_ids=["A2"],
        )])
        initial = self.work(institutions=[{"ror": ITMO_ROR_ID}])
        initial["authorships"].append({"author": {"id": "A2", "display_name": "Other Name"},
                                       "institutions": [{"ror": ITMO_ROR_ID}]})
        self.normalize(initial)
        initial["authorships"][0]["institutions"] = []
        corrected = self.normalize(initial)
        self.assertTrue(corrected.is_itmo)
        self.assertEqual({e.source_person_id for e in corrected.membership_evidence}, {"A2"})
        self.assertEqual({e.publication_id for e in corrected.membership_evidence}, {"W1"})

    def test_older_group_replay_cannot_restore_retracted_source_claim(self):
        self.normalize(self.work(institutions=[{"ror": ITMO_ROR_ID}]), "first",
                       datetime(2026, 10, 1, tzinfo=UTC))
        self.normalize(self.work(institutions=[]), "second", datetime(2026, 10, 2, tzinfo=UTC))
        OpenAlexNormalizer(self.raw, self.prepared).run()
        person = next(self.prepared.read_models("persons", Person))
        self.assertFalse(person.is_itmo)
        self.assertEqual(person.membership_status, "unsupported")
        self.assertEqual(person.membership_evidence, [])

    def test_equal_timestamp_conflict_preserves_current_claim_and_affiliation(self):
        observed = datetime(2026, 10, 1, tzinfo=UTC)
        first = self.work(institutions=[{"ror": ITMO_ROR_ID}])
        first["authorships"][0]["raw_affiliation_strings"] = ["ITMO University"]
        self.normalize(first, observed_at=observed)
        conflicting = self.work(institutions=[])
        conflicting["authorships"][0]["raw_affiliation_strings"] = ["Other University"]
        result = self.normalize(conflicting, "second", observed)
        self.assertTrue(result.is_itmo)
        self.assertEqual(result.authored[0].affiliation, "ITMO University")
        self.assertEqual(result.authored[0].institution_rors, [ITMO_ROR_ID])

    def test_normalization_after_merge_keeps_original_source_author(self):
        self.prepared.write_models("persons", [Person(
            id="A1", openalex_id="A1", is_itmo=False, merged_ids=["A2"],
        )])
        result = self.normalize(self.work(person_id="A2", institutions=[{"ror": ITMO_ROR_ID}]))
        self.assertEqual(result.id, "A1")
        self.assertEqual({e.source_person_id for e in result.membership_evidence}, {"A2"})
        self.assertEqual(result.authored[0].institution_source_person_id, "A2")

    def test_orcid_refresh_after_merge_does_not_reattribute_borrowed_evidence(self):
        orcid_id = "0000-0002-1825-0097"
        person = Person(id="A1", is_itmo=False, orcid=orcid_id, merged_ids=["A2"],
                        affiliations=[{"name": "ITMO", "ror": ITMO_ROR_ID, "source": "orcid",
                                       "source_person_id": "A2", "source_id": orcid_id}])
        apply_membership(person)
        self.prepared.write_models("persons", [person])
        payload = {"activities-summary": {"employments": {"affiliation-group": [{"summaries": [{
            "employment-summary": {"organization": {"name": "ITMO", "disambiguated-organization": {
                "disambiguation-source": "ROR", "disambiguated-organization-identifier": ITMO_ROR_ID,
            }}},
        }]}]}}}
        result = self.profile_stage(orcid=payload)
        self.assertTrue(result.is_itmo)
        self.assertEqual({e.source_person_id for e in result.membership_evidence}, {"A2"})

    def test_empty_orcid_refresh_keeps_original_owner_negative_coverage(self):
        orcid_id = "0000-0002-1825-0097"
        person = Person(
            id="A1", is_itmo=False, orcid=orcid_id, merged_ids=["A2"],
            name_raw="Original A", email="a@example.org", other_names=["A spelling"],
            authored=[Authorship(publication_id="W1", institution_rors=[],
                                institution_source_person_id="A1", institution_source_id="W1"),
                      Authorship(publication_id="W2", institution_rors=[],
                                institution_source_person_id="A2", institution_source_id="W2")],
            affiliations=[{"name": "ITMO", "ror": ITMO_ROR_ID, "source": "orcid",
                           "source_person_id": "A2", "source_id": orcid_id}],
        )
        apply_membership(person)
        self.prepared.write_models("persons", [person])
        graph = FakePanelGraph()
        load_prepared_rows(graph, {"persons.jsonl": [person.model_dump(mode="json")]})
        result = self.profile_stage(orcid={})
        self.assertEqual(result.membership_evidence, [])
        self.assertEqual(result.name_raw, person.name_raw)
        self.assertEqual(result.email, person.email)
        self.assertEqual(result.other_names, person.other_names)
        self.assertEqual({a.institution_source_person_id for a in result.authored}, {"A1", "A2"})
        self.assertTrue(any(source.source_person_id == "A2" and source.source_id == orcid_id
                            for source in result.membership_orcid_sources))
        load_prepared_rows(graph, {"persons.jsonl": [result.model_dump(mode="json")]})
        node = graph.nodes[("Person", "A1")]
        claims = json.loads(node["membership_evidence"])
        coverage = json.loads(node["membership_source_coverage"])
        self.assertFalse(any(claim.get("source") == "orcid" for claim in claims))
        self.assertTrue(any(source["source"] == "orcid" and source["source_person_id"] == "A2"
                            and source["source_id"] == orcid_id for source in coverage))
        self.assertEqual(node["membership_status"], "unknown")

    def test_dedup_and_unmerge_return_membership_evidence_to_its_owner(self):
        people = [
            Person(id="A1", openalex_id="A1", name_raw="Alex Sample", is_itmo=False,
                   orcid="0000-0002-1825-0097", authored=[Authorship(publication_id="W1",
                   institution_rors=[], institution_source_person_id="A1")]),
            Person(id="A2", openalex_id="A2", name_raw="A. Sample", is_itmo=False,
                   orcid="0000-0002-1825-0097", authored=[Authorship(publication_id="W2",
                   institution_rors=[ITMO_ROR_ID], institution_source_person_id="A2")]),
        ]
        for person in people:
            apply_membership(person)
        self.prepared.write_models("persons", people)
        self.prepared.write_models("publications", [Publication(id=w, title=w) for w in ("W1", "W2")])
        graph = FakePanelGraph()

        def publish():
            load_prepared_rows(graph, {filename: list(self.prepared.read_rows(entity))
                                      for entity, filename in ENTITY_FILES.items()})

        publish()
        DedupStage(self.prepared, self.raw, self.config)._dedup_persons()
        publish()
        merged = list(self.prepared.read_models("persons", Person))
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].is_itmo)
        self.assertEqual({e.source_person_id for e in merged[0].membership_evidence}, {"A2"})
        split_person(graph, self.db, ["A1", "A2"])
        restored = {p.id: p for p in self.prepared.read_models("persons", Person)}
        self.assertFalse(restored["A1"].is_itmo)
        self.assertEqual(restored["A1"].membership_evidence, [])
        self.assertTrue(restored["A2"].is_itmo)
        self.assertEqual({e.source_person_id for e in restored["A2"].membership_evidence}, {"A2"})
        publish()
        self.assertFalse(graph.nodes[("Person", "A1")]["is_itmo"])
        self.assertTrue(graph.nodes[("Person", "A2")]["is_itmo"])


class MembershipGraphParityTest(unittest.TestCase):
    def graphs(self):
        panel = FakePanelGraph()
        recording = RecordingNeo4jClient()
        return [(panel, lambda: panel.nodes[("Person", "A1")]),
                (recording, lambda: recording.nodes["Person"]["A1"])]

    def unsupported(self, **fields):
        return {"is_itmo": False, "membership_status": "unsupported",
                "membership_policy": POLICY_VERSION, "membership_evidence": [], **fields}

    def test_authoritative_downshift_and_stale_legacy_upsert_agree(self):
        for graph, person in self.graphs():
            with self.subTest(graph=type(graph).__name__):
                graph.upsert_person_nodes_batch([("A1", {"is_itmo": True})])
                graph.upsert_person_nodes_batch([("A1", self.unsupported())])
                self.assertFalse(person()["is_itmo"])
                graph.upsert_person_nodes_batch([("A1", {"is_itmo": True})])
                self.assertFalse(person()["is_itmo"])
                self.assertEqual(person()["membership_status"], "unsupported")

    def test_duplicate_rows_observe_previous_membership_update_in_same_batch(self):
        for graph, person in self.graphs():
            with self.subTest(graph=type(graph).__name__):
                graph.upsert_person_nodes_batch([
                    ("A1", {"is_itmo": True}), ("A1", self.unsupported()),
                    ("A1", {"is_itmo": True}),
                ])
                self.assertFalse(person()["is_itmo"])

    def test_partial_cluster_negative_does_not_downgrade_full_cluster(self):
        for graph, person in self.graphs():
            with self.subTest(graph=type(graph).__name__):
                graph.upsert_person_nodes_batch([("A1", {"is_itmo": True, "merged_ids": ["A2"]})])
                graph.upsert_person_nodes_batch([("A1", self.unsupported(merged_ids=[]))])
                self.assertTrue(person()["is_itmo"])
                self.assertEqual(person()["membership_status"], "unknown")
                self.assertEqual(person()["merged_ids"], ["A2"])

    def test_malformed_membership_claim_does_not_promote_external_person(self):
        for graph, person in self.graphs():
            for evidence in ([{}], [{"source": "openalex", "ror": ITMO_ROR_ID}],
                             [{"source": "orcid", "ror": "not-the-target"}]):
                with self.subTest(graph=type(graph).__name__, evidence=evidence):
                    graph.upsert_person_nodes_batch([("A1", {"is_itmo": False})])
                    graph.upsert_person_nodes_batch([("A1", {
                        "is_itmo": False, "membership_status": "supported",
                        "membership_policy": POLICY_VERSION, "membership_evidence": evidence,
                    })])
                    self.assertFalse(person()["is_itmo"])

    def test_downshift_preserves_physical_edges_but_filters_gui_publication(self):
        graph = FakePanelGraph()
        graph.upsert_person_nodes_batch([("A1", {"is_itmo": True})])
        graph.add("Publication", "W1")
        graph.add("Department", "D1")
        graph.upsert_relationships_batch("Person", "Publication", "AUTHORED", [("A1", "W1", {})])
        graph.upsert_relationships_batch("Person", "Department", "BELONGS_TO", [("A1", "D1", {})])
        before = dict(graph.relationships)
        load_prepared_rows(graph, {"persons.jsonl": [{"id": "A1", **self.unsupported()}]})
        self.assertEqual(graph.relationships, before)
        self.assertIn(("Person", "A1"), graph.nodes)
        self.assertFalse(graph.nodes[("Person", "A1")]["is_itmo"])
        index = build_authorship_index({
            "persons": [graph.nodes[("Person", "A1")]],
            "publications": [{"id": "W1"}], "authorship": [{"per": "A1", "pid": "W1"}],
        })
        self.assertEqual(index.pub_ids, set())


class PublicationMembershipFoldTest(unittest.TestCase):
    setUp = MembershipIntegrationTest.setUp
    work = MembershipIntegrationTest.work
    normalize = MembershipIntegrationTest.normalize

    def folded_work_versions(self):
        for work_id in ("W1", "W2"):
            payload = self.work(work_id, institutions=[{"ror": ITMO_ROR_ID}])
            payload.update(doi="https://doi.org/10.1234/one-publication", title="One publication")
            self.normalize(payload)
        mapping = DedupStage(self.prepared, self.raw, self.config)._dedup_publications()
        self.assertEqual(mapping, {"W2": "W1"})
        return next(self.prepared.read_models("persons", Person))

    def test_publication_fold_has_one_authorship_but_keeps_both_original_sources(self):
        person = self.folded_work_versions()
        self.assertEqual([(a.publication_id, a.position, a.institution_source_person_id)
                          for a in person.authored], [("W1", 1, "A1")])
        self.assertEqual({(e.source_person_id, e.source_id) for e in person.membership_evidence},
                         {("A1", "W1"), ("A1", "W2")})
        self.assertTrue(person.is_itmo)

    def test_correcting_one_folded_work_source_preserves_the_other_claim(self):
        self.folded_work_versions()
        corrected = self.work("W2", institutions=[])
        corrected.update(doi="https://doi.org/10.1234/one-publication", title="One publication")
        person = self.normalize(corrected)
        self.assertEqual({(e.source_person_id, e.source_id) for e in person.membership_evidence},
                         {("A1", "W1")})
        self.assertTrue(person.is_itmo)
        self.assertEqual([(a.publication_id, a.position) for a in person.authored], [("W1", 1)])

    def test_reordered_author_in_two_versions_has_one_display_and_independent_sources(self):
        works = []
        for work_id in ("W1", "W2"):
            payload = self.work(work_id, institutions=[{"ror": ITMO_ROR_ID}])
            payload["authorships"].append({"author": {"id": "A2", "display_name": "Other Author"},
                                            "institutions": []})
            if work_id == "W2":
                payload["authorships"].reverse()
            payload.update(doi="https://doi.org/10.1234/one-publication", title="One publication")
            self.normalize(payload)
            works.append(payload)
        self.assertEqual(DedupStage(self.prepared, self.raw, self.config)._dedup_publications(), {"W2": "W1"})
        person = next(p for p in self.prepared.read_models("persons", Person) if p.id == "A1")
        self.assertEqual({(e.source_id, e.position) for e in person.membership_evidence},
                         {("W1", 1), ("W2", 2)})
        self.assertEqual([a.publication_id for a in person.authored], ["W1"])
        works[1]["authorships"][1]["institutions"] = []
        self.normalize(works[1])
        person = next(p for p in self.prepared.read_models("persons", Person) if p.id == "A1")
        self.assertEqual({(e.source_id, e.position) for e in person.membership_evidence}, {("W1", 1)})
        self.assertTrue(person.is_itmo)
        self.assertEqual([a.publication_id for a in person.authored], ["W1"])

    def test_repeated_author_positions_within_one_raw_work_remain_distinct(self):
        payload = self.work(institutions=[{"ror": ITMO_ROR_ID}])
        payload["authorships"].append({"author": {"id": "A1", "display_name": "Alex Sample"},
                                        "institutions": []})
        person = self.normalize(payload)
        self.assertEqual([(a.publication_id, a.position) for a in person.authored], [("W1", 1), ("W1", 2)])
        self.assertEqual({(e.source_id, e.position) for e in person.membership_evidence}, {("W1", 1)})


class SharedAuthorshipMembershipTest(unittest.TestCase):
    setUp = MembershipIntegrationTest.setUp

    def shared_pair(self):
        people = [Person(
            id=pid, openalex_id=pid, name_raw=name, is_itmo=False,
            orcid="0000-0002-1825-0097", emails=[email],
            other_names=[f"Original {pid}"],
            authored=[Authorship(publication_id="W1", position=position,
                                affiliation=affiliation, institution_rors=rors,
                                institution_source_person_id=pid, institution_source_id="W1")],
        ) for pid, name, email, position, affiliation, rors in (
            ("A1", "Alex Sample", "first@example.org", 1, "Other University", []),
            ("A2", "A. Sample", "second@example.org", 2, "ITMO", [ITMO_ROR_ID]),
        )]
        for person in people:
            apply_membership(person)
        self.prepared.write_models("persons", people)
        self.prepared.write_models("publications", [Publication(id="W1", title="Shared work")])
        graph = MembershipPanelGraph()
        self.publish_to(graph)
        return graph, people

    def publish_to(self, graph):
        load_prepared_rows(graph, {filename: list(self.prepared.read_rows(entity))
                                  for entity, filename in ENTITY_FILES.items()})

    def test_shared_edge_and_base_fields_return_to_their_original_owners(self):
        graph, original = self.shared_pair()
        DedupStage(self.prepared, self.raw, self.config)._dedup_persons()
        self.publish_to(graph)
        graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")].update({
            "institution_rors": [ITMO_ROR_ID], "institution_source_person_id": None,
            "institution_source_id": "W1",
        })
        split_person(graph, self.db, ["A1", "A2"])
        restored = {p.id: p for p in self.prepared.read_models("persons", Person)}
        for expected in original:
            self.assertEqual(restored[expected.id].email, expected.email)
            self.assertEqual(restored[expected.id].emails, expected.emails)
            self.assertEqual(restored[expected.id].other_names, expected.other_names)
            self.assertEqual(restored[expected.id].membership_evidence, expected.membership_evidence)
            edge = graph.relationships[("Person", "AUTHORED", "Publication", expected.id, "W1")]
            self.assertEqual(edge.get("institution_rors"), expected.authored[0].institution_rors)
            self.assertEqual(edge.get("institution_source_person_id"), expected.id)
            self.assertEqual(edge.get("affiliation"), expected.authored[0].affiliation)

    def test_partial_republish_does_not_attribute_foreign_ror_to_canonical_author(self):
        graph, original = self.shared_pair()
        DedupStage(self.prepared, self.raw, self.config)._dedup_persons()
        self.publish_to(graph)
        graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")].update({
            "institution_rors": [ITMO_ROR_ID], "institution_source_person_id": None,
            "institution_source_id": "W1",
        })
        load_prepared_rows(graph, {"persons.jsonl": [original[0].model_dump(mode="json")]})
        edge = graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")]
        self.assertEqual(edge.get("institution_rors"), [ITMO_ROR_ID])
        self.assertFalse(ITMO_ROR_ID in (edge.get("institution_rors") or [])
                         and edge.get("institution_source_person_id") == "A1")


if __name__ == "__main__":
    unittest.main()
