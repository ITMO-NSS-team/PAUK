import unittest
from unittest.mock import patch

from pauk.graph.audit import AuditedNeo4jClient, actor_context


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

    def upsert_person_nodes_batch(self, nodes):
        self.calls.append(("upsert_person_nodes_batch", (nodes,)))

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


if __name__ == "__main__":
    unittest.main()
