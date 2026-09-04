import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph.audit import (
    AuditedNeo4jClient,
    AuditEntry,
    JSONLAuditSink,
    MongoAuditSink,
    MultiAuditSink,
    _storable,
    actor_context,
    build_audit_sink,
)
from pauk.settings import Settings


class FakeNeo4jClient:
    """Records calls instead of touching a real driver — no `driver`
    attribute on purpose, so a test fails loudly if AuditedNeo4jClient
    ever tries to reach through to a real session for something other
    than the two snapshot methods we patch below."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.relationships_matched = 0
        self.nodes_removed = 0

    def upsert_nodes_batch(self, labels, nodes):
        self.calls.append(("upsert_nodes_batch", (labels, nodes)))

    def upsert_person_nodes_batch(self, nodes, is_itmo):
        self.calls.append(("upsert_person_nodes_batch", (nodes, is_itmo)))

    def upsert_relationships_batch(self, src_label, tgt_label, rel_type, relationships, tgt_match_prop="id"):
        self.calls.append(("upsert_relationships_batch", (src_label, tgt_label, rel_type, relationships)))
        return self.relationships_matched

    def merge_person_nodes_batch(self, merges):
        self.calls.append(("merge_person_nodes_batch", (merges,)))
        return self.nodes_removed

    def merge_publication_nodes_batch(self, merges):
        self.calls.append(("merge_publication_nodes_batch", (merges,)))
        return self.nodes_removed

    def merge_repository_nodes_batch(self, merges):
        self.calls.append(("merge_repository_nodes_batch", (merges,)))
        return self.nodes_removed

    def promote_link_candidates_batch(self, candidates):
        self.calls.append(("promote_link_candidates_batch", (candidates,)))

    def fetch_persons_for_dedup(self):
        return [{"id": "p1"}]

    def close(self):
        self.calls.append(("close", ()))


class InMemorySink:
    def __init__(self):
        self.entries = []

    def write(self, entries):
        self.entries.extend(entries)


def audited_client(diff_threshold: int = 50) -> tuple[AuditedNeo4jClient, FakeNeo4jClient, InMemorySink]:
    fake = FakeNeo4jClient()
    sink = InMemorySink()
    return AuditedNeo4jClient(fake, sink, diff_threshold=diff_threshold), fake, sink


class PassthroughTest(unittest.TestCase):
    def test_read_only_methods_pass_through_untouched(self):
        client, fake, sink = audited_client()
        result = client.fetch_persons_for_dedup()
        self.assertEqual(result, [{"id": "p1"}])
        self.assertEqual(sink.entries, [])

    def test_close_passes_through(self):
        client, fake, _sink = audited_client()
        client.close()
        self.assertEqual(fake.calls, [("close", ())])


class UpsertNodesDiffTest(unittest.TestCase):
    def test_updated_field_is_diffed(self):
        client, fake, sink = audited_client()
        with patch.object(
            AuditedNeo4jClient, "_fetch_node_props",
            side_effect=[{"p1": {"email": "old@x.com"}}, {"p1": {"email": "new@x.com"}}],
        ), actor_context("user:alice", source="admin-ui"):
            client.upsert_nodes_batch("Person", [("p1", {"email": "new@x.com"})])

        self.assertEqual(len(sink.entries), 1)
        entry = sink.entries[0]
        self.assertEqual(entry.change_kind, "updated")
        self.assertEqual(entry.diff, {"email": ("old@x.com", "new@x.com")})
        self.assertEqual(entry.actor, "user:alice")
        self.assertEqual(entry.source, "admin-ui")
        self.assertEqual(entry.entity_type, "Person")
        self.assertEqual(entry.entity_id, "p1")
        # underlying client still received the call unchanged
        self.assertEqual(fake.calls, [("upsert_nodes_batch", ("Person", [("p1", {"email": "new@x.com"})]))])

    def test_new_node_is_created_not_updated(self):
        client, _fake, sink = audited_client()
        with patch.object(
            AuditedNeo4jClient, "_fetch_node_props",
            side_effect=[{}, {"p2": {"email": "new@x.com"}}],
        ):
            client.upsert_nodes_batch("Person", [("p2", {"email": "new@x.com"})])

        entry = sink.entries[0]
        self.assertEqual(entry.change_kind, "created")
        self.assertEqual(entry.diff, {"email": (None, "new@x.com")})

    def test_no_op_write_produces_no_entry(self):
        """MERGE ... SET n += {...} that changes nothing shouldn't spam the log."""
        client, _fake, sink = audited_client()
        same = {"email": "same@x.com"}
        with patch.object(AuditedNeo4jClient, "_fetch_node_props", side_effect=[{"p1": same}, {"p1": dict(same)}]):
            client.upsert_nodes_batch("Person", [("p1", same)])
        self.assertEqual(sink.entries, [])

    def test_empty_batch_short_circuits_without_touching_driver(self):
        client, fake, sink = audited_client()
        # No patch.object here on purpose: if this reached _fetch_node_props
        # it would try to open a real driver session and error out.
        client.upsert_nodes_batch("Person", [])
        self.assertEqual(fake.calls, [])
        self.assertEqual(sink.entries, [])

    def test_large_batch_emits_bulk_summary_not_per_row_diff(self):
        client, fake, sink = audited_client(diff_threshold=2)
        nodes = [("p1", {"a": 1}), ("p2", {"a": 1})]
        # No _fetch_node_props patch needed: threshold routes this to the
        # cheap summary path, which never calls it.
        client.upsert_nodes_batch("Person", nodes)

        self.assertEqual(len(sink.entries), 1)
        entry = sink.entries[0]
        self.assertEqual(entry.change_kind, "bulk")
        self.assertEqual(entry.diff, {})
        self.assertIn("2", entry.entity_id)
        self.assertEqual(fake.calls, [("upsert_nodes_batch", ("Person", nodes))])


class UpsertRelationshipsDiffTest(unittest.TestCase):
    def test_relationship_property_diff(self):
        client, fake, sink = audited_client()
        fake.relationships_matched = 1
        with patch.object(
            AuditedNeo4jClient, "_fetch_rel_props",
            side_effect=[{}, {"p1 -> pub1": {"position": 1}}],
        ):
            matched = client.upsert_relationships_batch(
                "Person", "Publication", "AUTHORED", [("p1", "pub1", {"position": 1})]
            )

        self.assertEqual(matched, 1)
        entry = sink.entries[0]
        self.assertEqual(entry.entity_type, "(Person)-[:AUTHORED]->(Publication)")
        self.assertEqual(entry.entity_id, "p1 -> pub1")
        self.assertEqual(entry.change_kind, "created")


class MergeNodesDiffTest(unittest.TestCase):
    def test_duplicate_disappears_canonical_gets_filled_field(self):
        client, fake, sink = audited_client()
        fake.nodes_removed = 1
        before = {
            "dup": {"email": "dup@x.com", "orcid": None},
            "canon": {"email": "canon@x.com", "orcid": None},
        }
        after = {
            # dup is gone after the fold
            "canon": {"email": "canon@x.com", "orcid": None},
        }
        with patch.object(AuditedNeo4jClient, "_fetch_node_props", side_effect=[before, after]):
            removed = client.merge_person_nodes_batch([("dup", "canon")])

        self.assertEqual(removed, 1)
        by_id = {e.entity_id: e for e in sink.entries}
        self.assertEqual(by_id["dup"].change_kind, "deleted")
        self.assertEqual(by_id["dup"].diff, {"email": ("dup@x.com", None)})
        self.assertNotIn("canon", by_id)  # canonical's props didn't change here, no entry

    def test_merge_fills_gap_on_canonical(self):
        client, _fake, sink = audited_client()
        before = {"dup": {"orcid": "0000-1"}, "canon": {"orcid": None}}
        after = {"canon": {"orcid": "0000-1"}}
        with patch.object(AuditedNeo4jClient, "_fetch_node_props", side_effect=[before, after]):
            client.merge_person_nodes_batch([("dup", "canon")])

        by_id = {e.entity_id: e for e in sink.entries}
        self.assertEqual(by_id["canon"].change_kind, "updated")
        self.assertEqual(by_id["canon"].diff, {"orcid": (None, "0000-1")})


class PromoteLinkCandidatesTest(unittest.TestCase):
    def test_promoted_candidate_is_logged_as_deleted(self):
        client, _fake, sink = audited_client()
        with patch.object(
            AuditedNeo4jClient, "_fetch_node_props",
            side_effect=[{"https://x.com/repo": {"url": "https://x.com/repo"}}, {}],
        ):
            client.promote_link_candidates_batch([("https://x.com/repo", "https://github.com/x/repo")])

        entry = sink.entries[0]
        self.assertEqual(entry.change_kind, "deleted")
        self.assertEqual(entry.entity_type, "LinkCandidate")


class FailureBehaviourTest(unittest.TestCase):
    def test_exception_in_underlying_call_writes_no_audit_entry(self):
        client, fake, sink = audited_client()

        def boom(*_a, **_kw):
            raise RuntimeError("neo4j is down")

        fake.upsert_nodes_batch = boom
        with patch.object(AuditedNeo4jClient, "_fetch_node_props", side_effect=[{"p1": {"a": 1}}]):  # noqa: SIM117
            with self.assertRaises(RuntimeError):
                client.upsert_nodes_batch("Person", [("p1", {"a": 2})])
        self.assertEqual(sink.entries, [])


class ActorContextTest(unittest.TestCase):
    def test_nested_context_restores_outer_actor(self):
        client, _fake, sink = audited_client()
        with actor_context("etl-pipeline", source="jsonl_loader"):
            with actor_context("user:bob", source="admin-ui"):  # noqa: SIM117
                with patch.object(AuditedNeo4jClient, "_fetch_node_props", side_effect=[{}, {"p1": {"a": 1}}]):
                    client.upsert_nodes_batch("Person", [("p1", {"a": 1})])
            with patch.object(AuditedNeo4jClient, "_fetch_node_props", side_effect=[{}, {"p2": {"a": 1}}]):
                client.upsert_nodes_batch("Person", [("p2", {"a": 1})])

        inner_entry, outer_entry = sink.entries
        self.assertEqual(inner_entry.actor, "user:bob")
        self.assertEqual(outer_entry.actor, "etl-pipeline")


class MongoAuditSinkTest(unittest.TestCase):
    """The sink the panel's change feed reads from."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.sink = MongoAuditSink(self.db)

    @staticmethod
    def entry(**overrides):
        fields = {
            "timestamp": "2026-08-14T10:00:00+00:00", "actor": "user:petrov",
            "source": "admin-ui", "operation": "upsert_nodes",
            "entity_type": "Person", "entity_id": "A1",
            "change_kind": "updated", "diff": {"name_ru": ("Ivanov I.", "Иванов И. И.")},
        }
        return AuditEntry(**{**fields, **overrides})

    def test_an_entry_is_stored_field_by_field(self):
        self.sink.write([self.entry()])
        (row,) = self.db.audit.find({}, {"_id": False})
        self.assertEqual(row["actor"], "user:petrov")
        self.assertEqual(row["entity_id"], "A1")
        # BSON has no tuple: readers get [old, new], same as from JSONL.
        self.assertEqual(row["diff"]["name_ru"], ["Ivanov I.", "Иванов И. И."])

    def test_nothing_is_written_for_an_empty_batch(self):
        self.sink.write([])
        self.assertEqual(self.db.audit.count_documents({}), 0)

    def test_a_value_mongo_cannot_store_is_kept_as_text(self):
        # Neo4j hands back its own types (DateTime and friends). The audit
        # path must not be the thing that fails a write.
        class Exotic:
            def __str__(self):
                return "2026-08-14T10:00:00"

        self.sink.write([self.entry(diff={"access_date": (None, Exotic())})])
        (row,) = self.db.audit.find({}, {"_id": False})
        self.assertEqual(row["diff"]["access_date"], [None, "2026-08-14T10:00:00"])

    def test_the_feed_can_be_read_by_entity_and_by_actor(self):
        self.sink.write([
            self.entry(entity_id="A1", actor="user:petrov"),
            self.entry(entity_id="A2", actor="user:ivanova"),
        ])
        self.assertEqual(self.db.audit.count_documents({"entity_type": "Person", "entity_id": "A1"}), 1)
        self.assertEqual(self.db.audit.count_documents({"actor": "user:ivanova"}), 1)


class MultiAuditSinkTest(unittest.TestCase):
    def test_every_sink_receives_the_batch(self):
        first, second = InMemorySink(), InMemorySink()
        entry = MongoAuditSinkTest.entry()
        MultiAuditSink(first, second).write([entry])
        self.assertEqual((len(first.entries), len(second.entries)), (1, 1))

    def test_a_failing_sink_is_not_swallowed(self):
        # Losing an audit record quietly is worse than a loud failure.
        class Broken:
            def write(self, entries):
                raise RuntimeError("mongo is down")

        with self.assertRaises(RuntimeError):
            MultiAuditSink(Broken(), InMemorySink()).write([MongoAuditSinkTest.entry()])


class DeleteAuditTest(unittest.TestCase):
    """Deletion is the one change nothing can reconstruct afterwards."""

    def setUp(self):
        self.fake = FakeNeo4jClient()
        self.fake.delete_nodes_batch = lambda label, ids, detach=True: len(ids)
        self.fake.delete_relationships_batch = lambda s, t, r, pairs, m="id": len(pairs)
        self.sink = InMemorySink()
        self.client = AuditedNeo4jClient(self.fake, self.sink)

    def test_a_deleted_node_is_recorded_with_its_last_values(self):
        with patch.object(AuditedNeo4jClient, "_fetch_node_props",
                          side_effect=[{"A1": {"id": "A1", "name_en": "Ivan"}}, {}]), \
             actor_context("user:petrov", source="admin-cli"):
            removed = self.client.delete_nodes_batch("Person", ["A1"])
        self.assertEqual(removed, 1)
        entry = self.sink.entries[0]
        self.assertEqual((entry.change_kind, entry.entity_id, entry.actor),
                         ("deleted", "A1", "user:petrov"))
        self.assertEqual(entry.diff["name_en"], ("Ivan", None))

    def test_a_large_delete_is_still_diffed_row_by_row(self):
        # Unlike upsert, deletion never collapses into a bulk summary: the
        # entry carrying the node's fields is the only record left of it.
        many = [f"A{i}" for i in range(60)]
        before = {node_id: {"id": node_id, "name_en": node_id} for node_id in many}
        with patch.object(AuditedNeo4jClient, "_fetch_node_props", side_effect=[before, {}]):
            self.client.delete_nodes_batch("Person", many)
        self.assertEqual(len(self.sink.entries), 60)

    def test_a_deleted_relationship_is_recorded(self):
        with patch.object(AuditedNeo4jClient, "_fetch_rel_props",
                          side_effect=[{"A1 -> W1": {"position": 1}}, {}]):
            removed = self.client.delete_relationships_batch(
                "Person", "Publication", "AUTHORED", [("A1", "W1")])
        self.assertEqual(removed, 1)
        entry = self.sink.entries[0]
        self.assertEqual(entry.entity_type, "(Person)-[:AUTHORED]->(Publication)")
        self.assertEqual(entry.change_kind, "deleted")

    def test_an_empty_delete_touches_neither_driver_nor_sink(self):
        self.assertEqual(self.client.delete_nodes_batch("Person", []), 0)
        self.assertEqual(self.client.delete_relationships_batch(
            "Person", "Publication", "AUTHORED", []), 0)
        self.assertEqual(self.sink.entries, [])


class SinkAssemblyTest(unittest.TestCase):
    """build_audit_sink is what every entry point uses to open the graph."""

    def test_without_a_database_only_the_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = build_audit_sink(Settings(data_dir=Path(tmp)))
            self.assertIsInstance(sink, JSONLAuditSink)
            self.assertTrue(sink.path.parent.exists())

    def test_with_a_database_both_sinks_receive_the_entry(self):
        db = mongomock.MongoClient()["pauk_test"]
        with tempfile.TemporaryDirectory() as tmp:
            sink = build_audit_sink(Settings(data_dir=Path(tmp)), db)
            sink.write([MongoAuditSinkTest.entry()])
            self.assertEqual(db.audit.count_documents({}), 1)
            written = (Path(tmp) / "audit" / "audit.jsonl")
            self.assertTrue(written.exists() and written.read_text(encoding="utf-8").strip())


class StorableTest(unittest.TestCase):
    def test_nested_values_survive(self):
        self.assertEqual(_storable(["a", 1, None]), ["a", 1, None])
        self.assertEqual(_storable({"k": ["v"]}), {"k": ["v"]})

    def test_keys_that_are_not_text_become_text(self):
        self.assertEqual(_storable({1: "a"}), {"1": "a"})


if __name__ == "__main__":
    unittest.main()

class FixedActorTest(unittest.TestCase):
    """An actor pinned to the client, for callers that span contexts."""

    def pinned(self, **who):
        fake, sink = FakeNeo4jClient(), InMemorySink()
        return AuditedNeo4jClient(fake, sink, **who), sink

    def test_a_pinned_actor_wins_over_the_context(self):
        # The panel opens its client in a dependency and edits in the
        # route — different contexts, so a contextvar set in the first is
        # not visible in the second, and every entry read "unknown".
        client, sink = self.pinned(actor="user:roman", source="admin-ui")
        with patch.object(AuditedNeo4jClient, "_fetch_node_props",
                          side_effect=[{"p1": {}}, {"p1": {"email": "new@x.com"}}]), \
             actor_context("someone-else", source="cli"):
            client.upsert_nodes_batch("Person", [("p1", {"email": "new@x.com"})])
        self.assertEqual([entry.actor for entry in sink.entries], ["user:roman"])
        self.assertEqual([entry.source for entry in sink.entries], ["admin-ui"])

    def test_without_a_pinned_actor_the_context_still_decides(self):
        # The CLI relies on this: it wraps its work in actor_context.
        client, sink = self.pinned()
        with patch.object(AuditedNeo4jClient, "_fetch_node_props",
                          side_effect=[{"p1": {}}, {"p1": {"email": "new@x.com"}}]), \
             actor_context("pipeline", source="publish"):
            client.upsert_nodes_batch("Person", [("p1", {"email": "new@x.com"})])
        self.assertEqual([entry.actor for entry in sink.entries], ["pipeline"])


class SinkFailureDoesNotUndoTheChangeTest(unittest.TestCase):
    """The journal records a change; it is not a condition for making one.

    Letting the sink raise meant a mutation already written to Neo4j came
    back as an error from inside `update_node`, before the caller could
    record its decision or put the graph back. The change stayed, with no
    override and no journal entry, and the request looked like it failed.
    """

    class DeadSink:
        def write(self, entries):
            raise RuntimeError("the sink is not writable")

    def audited(self):
        fake = FakeNeo4jClient()
        return AuditedNeo4jClient(fake, self.DeadSink()), fake

    def test_the_mutation_still_happens(self):
        client, fake = self.audited()
        with patch.object(AuditedNeo4jClient, "_fetch_node_props",
                          side_effect=[{}, {"p1": {"a": 1}}]):
            client.upsert_nodes_batch("Person", [("p1", {"a": 1})])
        self.assertEqual([name for name, _ in fake.calls], ["upsert_nodes_batch"])

    def test_nothing_is_raised_at_the_caller(self):
        client, _ = self.audited()
        with patch.object(AuditedNeo4jClient, "_fetch_node_props",
                          side_effect=[{}, {"p1": {"a": 1}}]):
            client.upsert_nodes_batch("Person", [("p1", {"a": 1})])

    def test_a_bulk_summary_survives_it_too(self):
        client, fake = self.audited()
        rows = [(f"p{n}", {"a": n}) for n in range(60)]
        client.upsert_nodes_batch("Person", rows)
        self.assertEqual([name for name, _ in fake.calls], ["upsert_nodes_batch"])

    def test_the_lost_entries_are_logged(self):
        # The only way anybody sees them afterwards.
        client, _ = self.audited()
        with patch.object(AuditedNeo4jClient, "_fetch_node_props",
                          side_effect=[{}, {"p1": {"a": 1}}]), \
                self.assertLogs("pauk.graph.audit", level="ERROR") as caught:
            client.upsert_nodes_batch("Person", [("p1", {"a": 1})])
        self.assertIn("audit not written", "".join(caught.output))

    def test_a_working_sink_still_gets_its_entries(self):
        client, _, sink = audited_client()
        with patch.object(AuditedNeo4jClient, "_fetch_node_props",
                          side_effect=[{}, {"p1": {"a": 1}}]):
            client.upsert_nodes_batch("Person", [("p1", {"a": 1})])
        self.assertEqual(len(sink.entries), 1)
