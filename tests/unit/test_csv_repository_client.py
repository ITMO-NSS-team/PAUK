import unittest
from unittest.mock import MagicMock, Mock

from neo4j.exceptions import ServiceUnavailable

from pauk.graph.client import Neo4jClient


class CsvRepositoryClientTest(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(Neo4jClient)
        self.client.driver = MagicMock()
        self.session = self.client.driver.session.return_value.__enter__.return_value
        self.tx = Mock()
        self.session.execute_write.side_effect = lambda operation: operation(self.tx)
        self.tx.run.return_value.fetch.return_value = [{"name": "Existing", "element_id": "element-1"}]

    def test_explicit_invalid_names_are_rejected_before_opening_a_session(self):
        for name in (None, "", " \t\n", 0, False, [], {}):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Repository.name"):
                self.client.upsert_csv_node(
                    "Repository", "R1", {"name": name, "description": "Changed"},
                )
        self.client.driver.session.assert_not_called()

    def test_explicit_valid_name_uses_the_existing_upsert_contract(self):
        self.client.upsert_nodes_batch = Mock()
        properties = {"name": " Kept verbatim ", "description": "Changed"}
        self.client.upsert_csv_node("Reviewed:Repository", "R1", properties)
        self.client.upsert_nodes_batch.assert_called_once_with(
            "Reviewed:Repository", [("R1", properties)],
        )
        self.client.driver.session.assert_not_called()

    def test_missing_name_updates_under_one_transaction_and_exact_labels(self):
        self.client.upsert_csv_node("Repository:Reviewed", "R1", {"description": "Changed"})
        self.session.execute_write.assert_called_once()
        self.assertEqual(self.tx.run.call_count, 2)
        lock, write = self.tx.run.call_args_list
        self.assertIn("MATCH (n:Repository:Reviewed {id: $node_id})", lock.args[0])
        self.assertIn("SET n.__pauk_edit_lock", lock.args[0])
        self.assertIn("REMOVE n.__pauk_edit_lock", lock.args[0])
        self.assertEqual(lock.kwargs, {"node_id": "R1"})
        self.assertIn("elementId(n) = $element_id", write.args[0])
        self.assertNotIn("$node_id", write.args[0])
        self.assertNotIn("MERGE", write.args[0])
        self.assertEqual(write.kwargs, {"element_id": "element-1", "properties": {"description": "Changed"}})
        self.tx.run.return_value.consume.assert_called_once()

    def test_missing_node_never_reaches_the_property_write(self):
        self.tx.run.return_value.fetch.return_value = []
        with self.assertRaisesRegex(ValueError, "Repository.name"):
            self.client.upsert_csv_node("Repository", "R1", {"description": "Changed"})
        self.assertEqual(self.tx.run.call_count, 1)

    def test_invalid_existing_name_never_reaches_the_property_write(self):
        for name in (None, "", " \t\n", 0, False, []):
            with self.subTest(name=name):
                self.tx.reset_mock()
                self.tx.run.return_value.fetch.return_value = [{"name": name, "element_id": "element-1"}]
                with self.assertRaisesRegex(ValueError, "Repository.name"):
                    self.client.upsert_csv_node("Repository", "R1", {"description": "Changed"})
                self.assertEqual(self.tx.run.call_count, 1)

    def test_partial_update_cannot_replace_identity_or_timestamps(self):
        properties = {"id": "R2", "created_at": "old", "updated_at": "old", "description": None}
        self.client.upsert_csv_node("Repository", "R1", properties)
        self.assertEqual(self.tx.run.call_args.kwargs, {
            "element_id": "element-1", "properties": {"description": None},
        })
        self.assertEqual(properties["id"], "R2")
        self.assertNotIn("name", self.tx.run.call_args.kwargs["properties"])

    def test_database_errors_are_not_converted_to_validation_errors(self):
        self.tx.run.side_effect = ServiceUnavailable("offline")
        with self.assertRaises(ServiceUnavailable):
            self.client.upsert_csv_node("Repository", "R1", {"description": "Changed"})

    def test_secondary_label_cannot_remove_repository_name(self):
        self.tx.run.return_value.fetch.return_value = [{"name": "Keep", "labels": ["Reviewed", "Repository"]}]
        with self.assertRaisesRegex(ValueError, "Repository.name"):
            self.client.upsert_csv_node("Reviewed", "R1", {"name": None, "description": "Changed"})
        self.assertEqual(self.tx.run.call_count, 1)
        self.assertIn("MERGE (n:Reviewed", self.tx.run.call_args.args[0])
        self.assertIn("labels(n)", self.tx.run.call_args.args[0])

    def test_secondary_label_repair_is_checked_under_the_same_transaction(self):
        self.tx.run.return_value.fetch.return_value = [{"name": None, "element_id": "element-1", "labels": ["Reviewed", "Repository"]}]
        self.client.upsert_csv_node("Reviewed", "R1", {"name": "Repaired"})
        self.session.execute_write.assert_called_once()
        self.assertEqual(self.tx.run.call_count, 2)
        self.assertEqual(self.tx.run.call_args.kwargs["properties"], {"name": "Repaired"})

    def test_non_repository_can_clear_its_optional_name(self):
        self.tx.run.return_value.fetch.return_value = [{"name": "Old", "element_id": "element-1", "labels": ["Organization"]}]
        self.client.upsert_csv_node("Organization", "O1", {"name": None})
        self.assertEqual(self.tx.run.call_count, 2)

    def test_ambiguous_secondary_label_does_not_write_any_properties(self):
        self.tx.run.return_value.fetch.return_value = [
            {"name": "First", "labels": ["Reviewed", "Repository"]},
            {"name": "Second", "labels": ["Reviewed", "Person"]},
        ]
        with self.assertRaisesRegex(ValueError, "more than one node"):
            self.client.upsert_csv_node("Reviewed", "R1", {"name": "Replacement"})
        self.assertEqual(self.tx.run.call_count, 1)
        self.tx.run.return_value.fetch.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
