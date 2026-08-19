import unittest

from pauk.graph.extract import NODE_REGISTRY
from pauk.graph.mutations import (
    NODE_FIELDS,
    RELATIONSHIPS,
    MutationError,
    NotFound,
    UnknownEntity,
    VersionConflict,
    create_node,
    create_relationship,
    delete_node,
    delete_relationship,
    merge_nodes,
    read_node,
    update_node,
    validate_fields,
    validate_relationship,
)


class FakeGraph:
    """An in-memory stand-in for Neo4jClient.

    Mirrors the parts manual edits rely on, including the `updated_at`
    the real client stamps on every write — the optimistic check is built
    on it, so a fake without it would let a broken check pass.
    """

    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}
        self.relationships: dict[tuple[str, str, str, str, str], dict] = {}
        self.clock = 0
        self.calls: list[str] = []

    def _tick(self) -> str:
        self.clock += 1
        return f"2026-08-14T10:00:{self.clock:02d}"

    def add(self, label: str, node_id: str, **props):
        self.nodes[(label, node_id)] = {"id": node_id, "updated_at": self._tick(), **props}

    def fetch_node_properties(self, label, node_id):
        found = self.nodes.get((label, node_id))
        return dict(found) if found else None

    def upsert_nodes_batch(self, labels, nodes):
        self.calls.append("upsert_nodes_batch")
        label = labels if isinstance(labels, str) else ":".join(labels)
        for node_id, props in nodes:
            row = self.nodes.setdefault((label, node_id), {"id": node_id})
            row.update({k: v for k, v in props.items()
                        if k not in ("id", "created_at", "updated_at")})
            row["updated_at"] = self._tick()

    def delete_nodes_batch(self, label, ids, detach=True):
        self.calls.append("delete_nodes_batch")
        removed = 0
        for node_id in ids:
            attached = [key for key in self.relationships
                        if (key[0] == label and key[3] == node_id)
                        or (key[2] == label and key[4] == node_id)]
            if attached and not detach:
                continue
            if self.nodes.pop((label, node_id), None) is not None:
                removed += 1
                for key in attached:
                    self.relationships.pop(key, None)
        return removed

    def upsert_relationships_batch(self, src_label, tgt_label, rel_type, relationships,
                                   tgt_match_prop="id"):
        self.calls.append("upsert_relationships_batch")
        matched = 0
        for src_id, tgt_id, props in relationships:
            target_exists = any(
                key[0] == tgt_label and value.get(tgt_match_prop) == tgt_id
                for key, value in self.nodes.items())
            if (src_label, src_id) not in self.nodes or not target_exists:
                continue
            self.relationships[(src_label, rel_type, tgt_label, src_id, tgt_id)] = dict(props)
            matched += 1
        return matched

    def delete_relationships_batch(self, src_label, tgt_label, rel_type, pairs,
                                   tgt_match_prop="id"):
        self.calls.append("delete_relationships_batch")
        removed = 0
        for src_id, tgt_id in pairs:
            if self.relationships.pop((src_label, rel_type, tgt_label, src_id, tgt_id), None) is not None:
                removed += 1
        return removed

    def merge_person_nodes_batch(self, merges):
        self.calls.append("merge_person_nodes_batch")
        removed = 0
        for duplicate_id, _canonical_id in merges:
            if self.nodes.pop(("Person", duplicate_id), None) is not None:
                removed += 1
        return removed

    # The rest of the loader's surface, so load_prepared_rows can run
    # against this fake end to end.

    def upsert_person_nodes_batch(self, nodes, is_itmo):
        self.calls.append("upsert_person_nodes_batch")
        self.upsert_nodes_batch("Person", nodes)

    def merge_publication_nodes_batch(self, merges):
        self.calls.append("merge_publication_nodes_batch")
        return 0

    def merge_repository_nodes_batch(self, merges):
        self.calls.append("merge_repository_nodes_batch")
        return 0

    def promote_link_candidates_batch(self, candidates):
        self.calls.append("promote_link_candidates_batch")

    def fetch_merged_id_map(self, label):
        return {}


class WhitelistTest(unittest.TestCase):
    """The closed sets that keep user input out of interpolated Cypher."""

    def test_every_label_the_loader_publishes_is_editable(self):
        # Compared against the registry itself, not a list written out here:
        # the graph grows (Organization arrived with department matching),
        # and a hand-kept copy would either fail on every such change or,
        # worse, quietly stop covering the new label.
        published = {spec.labels.split(":")[0] for spec in NODE_REGISTRY.values()}
        self.assertEqual(set(NODE_FIELDS), published)

    def test_the_labels_include_the_ones_the_panel_edits_most(self):
        # A guard against the check above passing on an empty registry.
        self.assertLessEqual({"Person", "Publication", "Repository", "Department"},
                             set(NODE_FIELDS))

    def test_a_person_field_from_either_registry_entry_is_editable(self):
        # Person is in the registry twice, ITMO and external; the two
        # field lists have to be unioned or half of them read as unknown.
        self.assertIn("degree", NODE_FIELDS["Person"])       # itmo_person only
        self.assertIn("orcid", NODE_FIELDS["Person"])        # both

    def test_the_offered_fields_never_include_ones_every_write_refuses(self):
        # created_at is published for Person by the loader but owned by the
        # database; offering it in `admin schema` would advertise an edit
        # that validate_fields then rejects.
        for label, fields in NODE_FIELDS.items():
            with self.subTest(label=label):
                self.assertEqual(fields & {"id", "created_at", "updated_at"}, frozenset())

    def test_an_unknown_label_is_refused(self):
        with self.assertRaises(UnknownEntity):
            validate_fields("Employee", {})

    def test_a_label_carrying_cypher_is_refused(self):
        # Labels are interpolated into the query text, so this is the
        # check standing between an HTTP request and an injection.
        with self.assertRaises(UnknownEntity):
            validate_fields("Person) DETACH DELETE (n", {"name_en": "x"})

    def test_a_field_the_loader_never_publishes_is_refused(self):
        with self.assertRaises(UnknownEntity):
            validate_fields("Person", {"salary": 100})

    def test_fields_the_database_owns_are_refused(self):
        for field in ("id", "created_at", "updated_at"):
            with self.subTest(field=field), self.assertRaises(UnknownEntity):
                validate_fields("Person", {field: "anything"})

    def test_a_known_relationship_returns_the_property_its_target_is_matched_by(self):
        self.assertEqual(validate_relationship("Person", "AUTHORED", "Publication"), "id")
        # Not every target is found by id.
        self.assertEqual(validate_relationship("Repository", "OWNED_BY", "GitHubProfile"), "login")

    def test_a_relationship_the_graph_does_not_have_is_refused(self):
        with self.assertRaises(UnknownEntity):
            validate_relationship("Person", "AUTHORED", "Department")

    def test_every_relationship_in_the_registry_is_offered(self):
        self.assertIn(("Person", "CONTRIBUTED_TO", "Repository"), RELATIONSHIPS)
        self.assertIn(("Publication", "MENTIONS_LINK", "Repository"), RELATIONSHIPS)


class NodeEditTest(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Ivan Petrov")

    def test_reading_a_missing_node_says_so(self):
        with self.assertRaises(NotFound):
            read_node(self.graph, "Person", "nobody")

    def test_creating_a_node_the_pipeline_does_not_know(self):
        created = create_node(self.graph, "Department", "D1", {"name_en": "New lab"})
        self.assertEqual(created["name_en"], "New lab")

    def test_creating_a_node_that_exists_is_refused(self):
        # Otherwise "create" would quietly become "overwrite".
        with self.assertRaises(MutationError):
            create_node(self.graph, "Person", "A1", {"name_en": "Someone else"})

    def test_updating_changes_only_the_fields_given(self):
        updated = update_node(self.graph, "Person", "A1", {"name_ru": "Иванов И. И."})
        self.assertEqual(updated["name_ru"], "Иванов И. И.")
        self.assertEqual(updated["name_en"], "Ivan Petrov")

    def test_an_edit_based_on_a_stale_read_is_refused(self):
        seen = read_node(self.graph, "Person", "A1")["updated_at"]
        update_node(self.graph, "Person", "A1", {"name_ru": "первый"})  # someone else saves
        with self.assertRaises(VersionConflict):
            update_node(self.graph, "Person", "A1", {"name_ru": "второй"}, expected_updated_at=seen)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "первый")

    def test_an_edit_matching_what_was_read_goes_through(self):
        seen = read_node(self.graph, "Person", "A1")["updated_at"]
        update_node(self.graph, "Person", "A1", {"name_ru": "Иванов"}, expected_updated_at=seen)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иванов")

    def test_deleting_a_lone_node(self):
        self.assertEqual(delete_node(self.graph, "Person", "A1"), 1)
        self.assertNotIn(("Person", "A1"), self.graph.nodes)

    def test_deleting_a_connected_node_needs_cascade(self):
        self.graph.add("Publication", "W1", title="paper")
        create_relationship(self.graph, "Person", "AUTHORED", "Publication", "A1", "W1")
        with self.assertRaises(MutationError):
            delete_node(self.graph, "Person", "A1")
        self.assertIn(("Person", "A1"), self.graph.nodes)
        self.assertEqual(delete_node(self.graph, "Person", "A1", cascade=True), 1)

    def test_deleting_a_missing_node_says_so(self):
        with self.assertRaises(NotFound):
            delete_node(self.graph, "Person", "nobody")


class RelationshipEditTest(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Ivan Petrov")
        self.graph.add("Publication", "W1", title="paper")

    def test_linking_two_nodes(self):
        self.assertEqual(
            create_relationship(self.graph, "Person", "AUTHORED", "Publication", "A1", "W1",
                                {"position": 1}), 1)
        self.assertEqual(
            self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")],
            {"position": 1})

    def test_linking_to_a_node_that_is_not_there(self):
        with self.assertRaises(NotFound):
            create_relationship(self.graph, "Person", "AUTHORED", "Publication", "A1", "missing")

    def test_unlinking_leaves_both_nodes(self):
        create_relationship(self.graph, "Person", "AUTHORED", "Publication", "A1", "W1")
        self.assertEqual(
            delete_relationship(self.graph, "Person", "AUTHORED", "Publication", "A1", "W1"), 1)
        self.assertIn(("Person", "A1"), self.graph.nodes)
        self.assertIn(("Publication", "W1"), self.graph.nodes)

    def test_unlinking_what_was_never_linked_removes_nothing(self):
        self.assertEqual(
            delete_relationship(self.graph, "Person", "AUTHORED", "Publication", "A1", "W1"), 0)


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Vitaly Aksenov")
        self.graph.add("Person", "A2", name_en="V. E. Aksenov")

    def test_the_survivor_records_the_id_it_swallowed(self):
        # Without merged_ids the loader recreates the duplicate on the very
        # next publish, and the merge silently undoes itself.
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.assertEqual(self.graph.nodes[("Person", "A1")]["merged_ids"], ["A2"])
        self.assertNotIn(("Person", "A2"), self.graph.nodes)

    def test_merged_ids_is_written_before_the_duplicate_disappears(self):
        # Reversed, a failure between the two steps would leave the
        # duplicate deleted and free to come back unnoticed.
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.assertLess(self.graph.calls.index("upsert_nodes_batch"),
                        self.graph.calls.index("merge_person_nodes_batch"))

    def test_a_second_merge_does_not_duplicate_the_entry(self):
        self.graph.nodes[("Person", "A1")]["merged_ids"] = ["A2"]
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.assertEqual(self.graph.nodes[("Person", "A1")]["merged_ids"], ["A2"])

    def test_a_label_the_graph_cannot_merge_is_refused(self):
        with self.assertRaises(UnknownEntity):
            merge_nodes(self.graph, "Department", "D1", "D2")

    def test_a_node_cannot_swallow_itself(self):
        with self.assertRaises(MutationError):
            merge_nodes(self.graph, "Person", "A1", "A1")

    def test_merging_something_that_is_not_there(self):
        with self.assertRaises(NotFound):
            merge_nodes(self.graph, "Person", "missing", "A1")


if __name__ == "__main__":
    unittest.main()
