import json
import unittest
from copy import deepcopy
from itertools import permutations, product
from unittest.mock import MagicMock

import mongomock

from pauk.graph.client import Neo4jClient, flatten_merges
from pauk.graph.dedup import dedup_graph_persons
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.models import Affiliation, Authorship, Person
from pauk.pipeline.normalize import OpenAlexNormalizer, _merge_person
from pauk.storage import PreparedStore, RawStore
from tests.bench.mocks import RecordingNeo4jClient


class PersonPreservationTest(unittest.TestCase):
    def test_republishing_only_survivor_keeps_all_merged_lists(self):
        client = RecordingNeo4jClient()
        rows = [Person(
            id=f"A{i}", is_member=True, name_raw="Ivan Petrov", orcid="same-orcid",
            other_names=[f"Alias {i}"], emails=[f"p{i}@example.org"],
            affiliations=[Affiliation(name=f"University {i}", source="openalex")],
        ).model_dump(mode="json") for i in (1, 2)]
        load_prepared_rows(client, {"persons.jsonl": deepcopy(rows)})
        self.assertEqual(dedup_graph_persons(client, {})[0], 1)
        before = deepcopy(client.nodes["Person"]["A1"])
        for _ in range(3):
            load_prepared_rows(client, {"persons.jsonl": [deepcopy(rows[0])]})
            self.assertEqual(client.nodes["Person"]["A1"], before)

    def test_unmerged_person_lists_still_allow_source_corrections(self):
        client = RecordingNeo4jClient()
        client.upsert_person_nodes_batch([("A1", {"emails": ["old@example.org"]})])
        client.upsert_person_nodes_batch([("A1", {"emails": []})])
        self.assertEqual(client.nodes["Person"]["A1"]["emails"], [])

    def test_merged_person_scalar_updates_are_not_frozen(self):
        client = RecordingNeo4jClient()
        client.upsert_person_nodes_batch([("A1", {"merged_ids": ["A2"], "name_raw": "Old", "emails": ["old@example.org"]})])
        client.upsert_person_nodes_batch([("A1", {"name_raw": "Corrected", "emails": []})])
        self.assertEqual(client.nodes["Person"]["A1"]["name_raw"], "Corrected")
        self.assertEqual(client.nodes["Person"]["A1"]["emails"], ["old@example.org"])

    def test_prepared_merge_does_not_alias_mutable_objects(self):
        base = Person(id="A1", is_member=True)
        extra = Person(id="A2", is_member=True, authored=[Authorship(publication_id="W1", position=1)])
        before = extra.model_dump()
        _merge_person(base, extra)
        base.authored[0].position = 99
        self.assertEqual(extra.model_dump(), before)

    def test_graph_projection_never_erases_unread_properties(self):
        client = RecordingNeo4jClient()
        for index in (1, 2, 3):
            client.upsert_person_nodes_batch(
                [
                    (
                        f"A{index}",
                        {
                            "is_member": True,
                            "name_raw": "Ivan Petrov",
                            "orcid": "0000-0001",
                            "emails": ["common@example.org", f"a{index}@example.org"],
                            "other_names": [f"Alias {index}"],
                            "affiliations": json.dumps([{"name": f"Institute {index}", "source": "openalex"}]),
                            "surname_en": "Petrov",
                            "biography": f"Profile {index}",
                            "custom_field": f"Value {index}",
                        },
                    )
                ]
            )
        client.nodes["Person"]["A2"]["homepage"] = "https://example.org/person"

        removed, _ = dedup_graph_persons(client, {})

        self.assertEqual(removed, 2)
        survivor = client.nodes["Person"]["A1"]
        self.assertEqual(
            survivor["emails"], ["common@example.org", "a1@example.org", "a2@example.org", "a3@example.org"]
        )
        self.assertEqual(survivor["other_names"], ["Alias 1", "Alias 2", "Alias 3"])
        self.assertEqual(len(json.loads(survivor["affiliations"])), 3)
        self.assertEqual(survivor["surname_en"], "Petrov")
        self.assertEqual(survivor["biography"], "Profile 1")
        self.assertEqual(survivor["custom_field"], "Value 1")
        self.assertEqual(survivor["homepage"], "https://example.org/person")
        self.assertEqual(set(survivor["merged_ids"]), {"A2", "A3"})
        before = dict(survivor)
        self.assertEqual(dedup_graph_persons(client, {})[0], 0)
        self.assertEqual(survivor, before)

    def test_missing_graph_fields_are_not_written_as_empty_lists(self):
        client = RecordingNeo4jClient()
        client.upsert_person_nodes_batch(
            [(pid, {"name_raw": "Ivan Petrov", "orcid": "0000-0001"}) for pid in ("A1", "A2")]
        )
        dedup_graph_persons(client, {})
        survivor = client.nodes["Person"]["A1"]
        for field in ("emails", "affiliations", "other_names"):
            self.assertNotIn(field, survivor)

    def test_distinct_primary_emails_are_retained_in_the_email_list(self):
        client = RecordingNeo4jClient()
        client.upsert_person_nodes_batch(
            [
                (pid, {"name_raw": "Ivan Petrov", "orcid": "0000-0001", "email": email})
                for pid, email in (("A1", "first@example.org"), ("A2", "second@example.org"))
            ]
        )
        # This tests applying an approved merge, not bypassing the resolver's
        # conflict guard for different primary addresses.
        client.merge_person_nodes_batch([("A2", "A1")])
        survivor = client.nodes["Person"]["A1"]
        self.assertEqual(survivor["email"], "first@example.org")
        self.assertEqual(survivor["emails"], ["first@example.org", "second@example.org"])
        base = Person(id="A1", is_member=True, email="first@example.org")
        extra = Person(id="A2", is_member=True, email="second@example.org")
        _merge_person(base, extra)
        self.assertEqual(base.emails, survivor["emails"])

    def test_prepared_merge_preserves_all_lists_and_fills_missing_scalars(self):
        base = Person(
            id="A1",
            openalex_id="A1",
            is_member=False,
            emails=["first@example.org"],
            other_names=["Alias 1"],
            affiliations=[Affiliation(name="First", source="openalex")],
            biography="Keep this",
            works_count=0,
            authored=[Authorship(publication_id="W1", is_corresponding=True)],
        )
        extra = Person(
            id="A2",
            openalex_id="A2",
            is_member=True,
            emails=["first@example.org", "second@example.org"],
            other_names=["Alias 2"],
            affiliations=[Affiliation(name="Second", source="orcid")],
            first_name_en="Ivan",
            surname_en="Petrov",
            homepage="https://example.org",
            biography="Do not replace",
            works_count=10,
            authored=[Authorship(publication_id="W1", is_corresponding=False)],
        )
        _merge_person(base, extra)
        before = base.model_dump()
        _merge_person(base, extra)
        self.assertEqual(base.model_dump(), before)
        self.assertEqual((base.id, base.openalex_id), ("A1", "A1"))
        self.assertTrue(base.is_member)
        self.assertEqual(base.emails, ["first@example.org", "second@example.org"])
        self.assertEqual(base.other_names, ["Alias 1", "Alias 2"])
        self.assertEqual([a.name for a in base.affiliations], ["First", "Second"])
        self.assertEqual((base.first_name_en, base.surname_en), ("Ivan", "Petrov"))
        self.assertEqual(base.homepage, "https://example.org")
        self.assertEqual((base.biography, base.works_count), ("Keep this", 0))
        self.assertEqual([a.is_corresponding for a in base.authored], [True, False])


class CorrespondingPreservationTest(unittest.TestCase):
    def test_publish_and_both_fold_directions_preserve_any_positive_record(self):
        for mode, first, second in product(
            ("publish", "person", "publication"), (None, False, True), (None, False, True)
        ):
            with self.subTest(mode=mode, first=first, second=second):
                client = RecordingNeo4jClient()
                client.upsert_person_nodes_batch([("A1", {}), ("A2", {})])
                client.upsert_nodes_batch("Publication", [("W1", {}), ("W2", {})])
                rows = [
                    ("A1", "W1", {} if first is None else {"is_corresponding": first}),
                    (
                        "A2" if mode == "person" else "A1",
                        "W2" if mode == "publication" else "W1",
                        {} if second is None else {"is_corresponding": second},
                    ),
                ]
                client.upsert_relationships_batch("Person", "Publication", "AUTHORED", rows)
                if mode == "person":
                    client.merge_person_nodes_batch([("A2", "A1")])
                elif mode == "publication":
                    client.merge_publication_nodes_batch([("W2", "W1")])
                props = client.edges[("Person", "AUTHORED", "Publication", "A1", "W1")]
                expected = None if first is None and second is None else bool(first or second)
                self.assertEqual(props.get("is_corresponding"), expected)

    def test_republishing_conflicting_prepared_rows_keeps_corresponding(self):
        client = RecordingNeo4jClient()
        client.upsert_nodes_batch("Publication", [("W1", {})])
        for flags in ((True, False), (False, True), (False,)):
            load_prepared_rows(
                client,
                {
                    "persons.jsonl": [
                        {
                            "id": "A1",
                            "is_member": True,
                            "authored": [{"publication_id": "W1", "is_corresponding": flag} for flag in flags],
                        }
                    ]
                },
            )
            self.assertTrue(client.edges[("Person", "AUTHORED", "Publication", "A1", "W1")]["is_corresponding"])

    def test_normalization_does_not_erase_positive_authorship(self):
        for flags in ((True, False), (False, True)):
            with self.subTest(flags=flags):
                db = mongomock.MongoClient().db
                raw, prepared = RawStore(db, "test"), PreparedStore(db, "test")
                for flag in flags:
                    raw.append(
                        "openalex_works",
                        {
                            "id": "https://openalex.org/W1",
                            "title": "Work",
                            "authorships": [
                                {
                                    "author": {"id": "https://openalex.org/A1", "display_name": "Ivan Petrov"},
                                    "is_corresponding": flag,
                                }
                            ],
                        },
                        {},
                    )
                OpenAlexNormalizer(raw, prepared).run()
                person = next(prepared.read_models("persons", Person))
                self.assertTrue(person.authored[0].is_corresponding)

    def test_real_client_generates_or_for_authored_only(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        session = client.driver.session.return_value.__enter__.return_value
        tx = MagicMock()
        tx.run.return_value.single.return_value = {"matched": 1, "removed": 1}
        tx.run.return_value.__iter__.return_value = iter([])
        session.execute_write.side_effect = lambda action: action(tx)

        client.merge_person_nodes_batch([("A2", "A1")])
        client.merge_publication_nodes_batch([("W2", "W1")])
        client.upsert_relationships_batch("Person", "Publication", "AUTHORED", [("A1", "W1", {})])
        queries = [call.args[0] for call in tx.run.call_args_list]
        authored_queries = [q for q in queries if "MERGE" in q and ":AUTHORED" in q]
        self.assertEqual(len(authored_queries), 3)
        for query in authored_queries:
            self.assertIn(" OR ", query)
            self.assertIn("is_corresponding", query)
        for query in queries:
            if ":AUTHORED" not in query:
                self.assertNotIn("SET new.is_corresponding", query)

    def test_real_client_combines_repeated_pairs_before_writing(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        session = client.driver.session.return_value.__enter__.return_value
        tx = MagicMock()
        tx.run.return_value.single.return_value = {"matched": 3}
        session.execute_write.side_effect = lambda action: action(tx)
        for flags in ((True, False, False), (False, False, True)):
            with self.subTest(flags=flags):
                result = client.upsert_relationships_batch(
                    "Person",
                    "Publication",
                    "AUTHORED",
                    [("A1", "W1", {"is_corresponding": flag}) for flag in flags],
                )
                self.assertEqual(result, 3)
                batch = tx.run.call_args.kwargs["batch"]
                self.assertEqual(len(batch), 1)
                self.assertEqual(batch[0]["input_count"], 3)
                self.assertTrue(batch[0]["rel_properties"]["is_corresponding"])


class MergeAliasSafetyTest(unittest.TestCase):
    def test_every_chain_order_resolves_to_the_same_survivor(self):
        pairs = [("A", "B"), ("B", "C"), ("D", "B")]
        for order in permutations(pairs):
            with self.subTest(order=order):
                flattened = flatten_merges(list(order))
                self.assertEqual(dict(flattened), {"A": "C", "B": "C", "D": "C"})
                self.assertEqual(flattened[0], ("B", "C"))

    def test_overlapping_aliases_are_safe_only_if_they_converge(self):
        self.assertEqual(dict(flatten_merges([("A", "B"), ("A", "C"), ("B", "C")])), {"A": "C", "B": "C"})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            flatten_merges([("A", "B"), ("A", "C")])

    def test_cycles_are_rejected_and_self_pairs_are_ignored(self):
        self.assertEqual(flatten_merges([("A", "A"), ("B", "A"), ("B", "A")]), [("B", "A")])
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            flatten_merges([("A", "B"), ("B", "A")])

    def test_long_chain_does_not_depend_on_recursion_or_chunk_size(self):
        count = 4001
        result = flatten_merges([(str(i), str(i + 1)) for i in range(count)])
        self.assertEqual(len(result), count)
        self.assertEqual({canonical for _alias, canonical in result}, {str(count)})

    def test_invalid_loader_plan_is_rejected_before_any_writes(self):
        for pairs in ([('A', 'B'), ('B', 'A')], [('A', 'B'), ('A', 'C')]):
            with self.subTest(pairs=pairs):
                client = RecordingNeo4jClient()
                rows = [{"id": canonical, "merged_ids": [alias]} for alias, canonical in pairs]
                with self.assertRaises(ValueError):
                    load_prepared_rows(client, {"persons.jsonl": rows})
                self.assertFalse(any(client.nodes.values()))
                self.assertFalse(client.edges)

    def test_loader_keeps_old_id_relationships_until_the_fold(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, {
            "publications.jsonl": [{"id": "W1", "title": "Work"}],
            "persons.jsonl": [
                {"id": "A", "authored": [{"publication_id": "W1", "is_corresponding": True}]},
                {"id": "B", "merged_ids": ["A"]},
                {"id": "C", "merged_ids": ["B"]},
            ],
        })
        self.assertEqual(set(client.nodes["Person"]), {"C"})
        self.assertEqual(set(client.nodes["Person"]["C"]["merged_ids"]), {"A", "B"})
        self.assertTrue(client.edges[("Person", "AUTHORED", "Publication", "C", "W1")]["is_corresponding"])
