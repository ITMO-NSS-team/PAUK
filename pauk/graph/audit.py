"""Audit wrapper around Neo4jClient.

Mutations are recorded from before/after snapshots; read methods pass through.
Actor and source come from actor_context or explicit wrapper arguments. Batches
at or above diff_threshold produce one summary to avoid expensive per-row diffs.

Audit writes happen after graph commits and cannot be atomic with them.
A crash may leave a graph change without an entry; sink failures are logged
without failing an already committed mutation.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, Protocol, cast

from pymongo.database import Database

from pauk.settings import Settings

from .client import Neo4jClient

logger = logging.getLogger(__name__)

_actor_var: ContextVar[str] = ContextVar("audit_actor", default="unknown")
_source_var: ContextVar[str] = ContextVar("audit_source", default="unknown")

TECHNICAL_DIFF_FIELDS = {"created_at", "updated_at"}


class actor_context:
    """Set the acting user/system for every audited write inside the block.

    Nestable: an inner block's actor/source apply only for its own scope
    and the outer one is restored on exit. Safe across threads and
    asyncio tasks (contextvars are task-local).
    """

    def __init__(self, actor: str, source: str | None = None):
        self.actor = actor
        self.source = source or actor

    def __enter__(self) -> actor_context:
        self._actor_token = _actor_var.set(self.actor)
        self._source_token = _source_var.set(self.source)
        return self

    def __exit__(self, *exc_info) -> None:
        _actor_var.reset(self._actor_token)
        _source_var.reset(self._source_token)


def set_actor(actor: str, source: str | None = None) -> None:
    """Name the actor for everything this context audits from now on.

    The block form (`actor_context`) restores the previous actor on exit,
    which needs the entry and the exit to happen in one context. A
    generator dependency in FastAPI is entered and resumed in different
    ones, and resetting a token across that boundary raises. Callers in
    that position set the actor and let the next one overwrite it.
    """
    _actor_var.set(actor)
    _source_var.set(source or actor)


@dataclass(frozen=True)
class AuditEntry:
    """One audited change, ready to hand to an AuditSink."""

    timestamp: str
    actor: str
    source: str
    operation: str  # e.g. "upsert_nodes", "upsert_relationships", "merge_nodes"
    entity_type: str  # node label(s), or "(Src)-[REL]->(Tgt)" for relationships
    entity_id: str  # node id, or "src_id -> tgt_id" for relationships
    change_kind: str  # "created" | "updated" | "deleted" | "bulk" (summary only, diff is empty)
    diff: dict[str, tuple[Any, Any]]  # field -> (old, new); {} for bulk summaries


class AuditSink(Protocol):
    def write(self, entries: Sequence[AuditEntry]) -> None: ...


class JSONLAuditSink:
    """Append-only JSONL sink, one line per AuditEntry.

    Easy to grep and tail; AuditedNeo4jClient only knows the AuditSink protocol.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, entries: Sequence[AuditEntry]) -> None:
        if not entries:
            return
        lines = [
            json.dumps(
                {
                    "timestamp": e.timestamp,
                    "actor": e.actor,
                    "source": e.source,
                    "operation": e.operation,
                    "entity_type": e.entity_type,
                    "entity_id": e.entity_id,
                    "change_kind": e.change_kind,
                    "diff": {k: list(v) for k, v in e.diff.items()},
                },
                ensure_ascii=False,
                default=str,  # neo4j DateTime etc. -> str, never crash the audit path
            )
            for e in entries
        ]
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def _storable(value: Any) -> Any:
    """Convert driver-specific values to text when BSON cannot encode them."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_storable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _storable(item) for key, item in value.items()}
    return str(value)


class MongoAuditSink:
    """Store entries in MongoDB for the admin feed's filters and pagination."""

    COLLECTION = "audit"

    def __init__(self, db: Database) -> None:
        self.db = db

    def write(self, entries: Sequence[AuditEntry]) -> None:
        if not entries:
            return
        self.db[self.COLLECTION].insert_many([
            {
                "timestamp": entry.timestamp,
                "actor": entry.actor,
                "source": entry.source,
                "operation": entry.operation,
                "entity_type": entry.entity_type,
                "entity_id": entry.entity_id,
                "change_kind": entry.change_kind,
                # Tuples become two-element lists: BSON has no tuple type,
                # and readers already expect [old, new] from the JSONL sink.
                "diff": {field: [_storable(old), _storable(new)]
                         for field, (old, new) in entry.diff.items()},
            }
            for entry in entries
        ])


class MultiAuditSink:
    """Write to each sink in order; an exception skips the remaining sinks."""

    def __init__(self, *sinks: AuditSink) -> None:
        self.sinks = sinks

    def write(self, entries: Sequence[AuditEntry]) -> None:
        for sink in self.sinks:
            sink.write(entries)


def build_audit_sink(config: Settings, db: Database | None = None) -> AuditSink:
    """Write JSONL audit logs and, when a database is provided, the admin feed."""
    config.audit_dir.mkdir(parents=True, exist_ok=True)
    jsonl = JSONLAuditSink(config.audit_dir / "audit.jsonl")
    return MultiAuditSink(jsonl, MongoAuditSink(db)) if db is not None else jsonl


def audited_client(config: Settings, db: Database | None = None,
                   actor: str | None = None, source: str | None = None,
                   **driver_options) -> AuditedNeo4jClient:
    """An open Neo4j client that records what it changes.

    Callers own the returned client and must close() it, same as with a
    plain Neo4jClient.

    Args:
        actor: Name to record for every change this client makes. Leave it
            out inside a CLI command and wrap the work in `actor_context`
            instead; pass it where the work spans contexts, such as a web
            request, because a contextvar set in a dependency is not
            visible in the route.
        source: Where the changes come from, e.g. "admin-ui".
    """
    raw = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password,
                      **driver_options)
    return AuditedNeo4jClient(raw, build_audit_sink(config, db), actor=actor, source=source)


def _diff_props(before: dict | None, after: dict | None) -> tuple[dict[str, tuple[Any, Any]], str]:
    """Field-level diff between two property maps for the same entity id."""
    if before is None:
        keys = (after or {}).keys() - TECHNICAL_DIFF_FIELDS
        return {k: (None, after[k]) for k in keys if after[k] is not None}, "created"
    if after is None:
        keys = before.keys() - TECHNICAL_DIFF_FIELDS
        return {k: (before[k], None) for k in keys if before[k] is not None}, "deleted"
    keys = (before.keys() | after.keys()) - TECHNICAL_DIFF_FIELDS
    diff = {k: (before.get(k), after.get(k)) for k in keys if before.get(k) != after.get(k)}
    return diff, "updated"


class AuditedNeo4jClient:
    """Audit-logging wrapper around Neo4jClient.

    Delegates every method it doesn't explicitly override to the wrapped client
    (fetch_*, close, driver access, ...). Mutating methods are intercepted:
    snapshot -> underlying call -> snapshot -> diff -> sink.
    """

    def __init__(self, client: Neo4jClient, sink: AuditSink, diff_threshold: int = 50,
                 actor: str | None = None, source: str | None = None):
        """
        Args:
            client: The real Neo4jClient to wrap.
            sink: Where audit entries go.
            diff_threshold: Batches with this many rows or more get one coarse summary entry instead of a per-row
                diff.
            actor: Who to record as the author, fixed for this client's
                lifetime. Callers that cannot rely on `actor_context` pass
                it here: a web request enters its dependency in one
                context and runs the route in another, and a contextvar
                does not cross that boundary, so entries would be
                recorded as "unknown".
            source: Where the change came from, alongside `actor`.
        """
        self._client = client
        self._sink = sink
        self._diff_threshold = diff_threshold
        self._actor = actor
        self._source = source

    def _who(self) -> tuple[str, str]:
        """The actor to record: the fixed one if set, else the context's."""
        if self._actor is not None:
            return self._actor, self._source or self._actor
        return _actor_var.get(), _source_var.get()

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    
    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _record(self, entries: Sequence[AuditEntry]) -> None:
        """Log sink failures without failing a committed graph mutation.

        The exception log includes the lost entries for recovery.
        """
        try:
            self._sink.write(entries)
        except Exception:
            logger.exception("audit not written, %d entr(y/ies) lost: %s",
                             len(entries), entries)

    def _emit_bulk_summary(self, operation: str, entity_type: str, row_count: int) -> None:
        self._record(
            [
                AuditEntry(
                    timestamp=self._now(),
                    actor=self._who()[0],
                    source=self._who()[1],
                    operation=operation,
                    entity_type=entity_type,
                    entity_id=f"<bulk: {row_count} rows>",
                    change_kind="bulk",
                    diff={},
                )
            ]
        )

    def _emit_diffs(
        self,
        operation: str,
        entity_type: str,
        before_by_id: dict[str, dict],
        after_by_id: dict[str, dict],
    ) -> None:
        now = self._now()
        actor, source = self._who()
        entries = []
        for entity_id in before_by_id.keys() | after_by_id.keys():
            diff, kind = _diff_props(before_by_id.get(entity_id), after_by_id.get(entity_id))
            if not diff and kind == "updated":
                # Creation/deletion still needs an entry when the entity has no properties.
                continue
            entries.append(AuditEntry(now, actor, source, operation, entity_type, entity_id, kind, diff))
        self._record(entries)

    def _fetch_node_props(self, labels: str | list[str], ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        label_list = labels if isinstance(labels, list) else [labels]
        # Match any existing label so adding another label is audited as an update.
        label_match = " OR ".join(f"n:{label}" for label in label_list)
        query = cast(
            LiteralString,
            f"MATCH (n) WHERE ({label_match}) AND n.id IN $ids RETURN n.id AS id, properties(n) AS props",
        )
        with self._client.driver.session() as session:
            rows = session.execute_read(lambda tx: [dict(r) for r in tx.run(query, ids=ids)])
        return {r["id"]: r["props"] for r in rows}

    def _fetch_rel_props(
        self, src_label: str, tgt_label: str, rel_type: str, tgt_match_prop: str, pairs: list[tuple[str, str]]
    ) -> dict[str, dict]:
        if not pairs:
            return {}
        batch = [{"src_id": s, "tgt_id": t} for s, t in pairs]
        query = cast(
            LiteralString,
            f"""
            UNWIND $batch AS row
            MATCH (src:{src_label} {{id: row.src_id}})-[r:{rel_type}]->(tgt:{tgt_label} {{{tgt_match_prop}: row.tgt_id}})
            RETURN row.src_id AS src_id, row.tgt_id AS tgt_id, properties(r) AS props
            """,
        )
        with self._client.driver.session() as session:
            rows = session.execute_read(lambda tx: [dict(r) for r in tx.run(query, batch=batch)])
        return {f"{r['src_id']} -> {r['tgt_id']}": r["props"] for r in rows}

    def upsert_nodes_batch(self, labels, nodes: list[tuple[str, dict]]):
        if not nodes:
            return
        label_list = labels if isinstance(labels, list) else [labels]
        label_str = ":".join(label_list)
        if len(nodes) >= self._diff_threshold:
            self._client.upsert_nodes_batch(labels, nodes)
            self._emit_bulk_summary("upsert_nodes", label_str, len(nodes))
            return
        ids = [node_id for node_id, _ in nodes]
        before = self._fetch_node_props(label_list, ids)
        self._client.upsert_nodes_batch(labels, nodes)
        after = self._fetch_node_props(label_list, ids)
        self._emit_diffs("upsert_nodes", label_str, before, after)

    def update_node_properties(self, label, node_id, properties, expected_updated_at=None):
        before, after = self._client.update_node_properties(
            label, node_id, properties, expected_updated_at)
        if after is not None:
            self._emit_diffs("upsert_nodes", label, {node_id: before}, {node_id: after})
        return before, after

    def upsert_person_nodes_batch(self, nodes: list[tuple[str, dict]]):
        if not nodes:
            return
        if len(nodes) >= self._diff_threshold:
            self._client.upsert_person_nodes_batch(nodes)
            self._emit_bulk_summary("upsert_person_nodes", "Person", len(nodes))
            return
        ids = [node_id for node_id, _ in nodes]
        before = self._fetch_node_props("Person", ids)
        self._client.upsert_person_nodes_batch(nodes)
        after = self._fetch_node_props("Person", ids)
        self._emit_diffs("upsert_person_nodes", "Person", before, after)

    def upsert_relationships_batch(
        self,
        src_label: str,
        tgt_label: str,
        rel_type: str,
        relationships: list[tuple[str, str, dict]],
        tgt_match_prop: str = "id",
    ) -> int:
        if not relationships:
            return 0
        entity_type = f"({src_label})-[:{rel_type}]->({tgt_label})"
        if len(relationships) >= self._diff_threshold:
            matched = self._client.upsert_relationships_batch(
                src_label, tgt_label, rel_type, relationships, tgt_match_prop
            )
            self._emit_bulk_summary("upsert_relationships", entity_type, len(relationships))
            return matched
        pairs = [(s, t) for s, t, _ in relationships]
        before = self._fetch_rel_props(src_label, tgt_label, rel_type, tgt_match_prop, pairs)
        matched = self._client.upsert_relationships_batch(src_label, tgt_label, rel_type, relationships, tgt_match_prop)
        after = self._fetch_rel_props(src_label, tgt_label, rel_type, tgt_match_prop, pairs)
        self._emit_diffs("upsert_relationships", entity_type, before, after)
        return matched

    def set_relationship_properties_batch(
        self, src_label: str, tgt_label: str, rel_type: str,
        relationships: list[tuple[str, str, dict]], tgt_match_prop: str = "id",
    ) -> int:
        if not relationships:
            return 0
        entity_type = f"({src_label})-[:{rel_type}]->({tgt_label})"
        pairs = [(src, tgt) for src, tgt, _props in relationships]
        before = self._fetch_rel_props(src_label, tgt_label, rel_type, tgt_match_prop, pairs)
        matched = self._client.set_relationship_properties_batch(
            src_label, tgt_label, rel_type, relationships, tgt_match_prop)
        after = self._fetch_rel_props(src_label, tgt_label, rel_type, tgt_match_prop, pairs)
        self._emit_diffs("set_relationship_properties", entity_type, before, after)
        return matched

    def sync_mentions_links_batch(
        self, publications: list[tuple[str, list[str], list[str]]]
    ) -> int:
        if not publications:
            return 0
        removed = self._client.sync_mentions_links_batch(publications)
        if removed:
            self._emit_bulk_summary(
                "sync_mentions_links", "(Publication)-[:MENTIONS_LINK]->()", removed,
            )
        return removed

    def sync_implements_relationships_batch(
        self, publications: list[tuple[str, list[str]]]
    ) -> int:
        if not publications:
            return 0
        removed = self._client.sync_implements_relationships_batch(publications)
        if removed:
            self._emit_bulk_summary(
                "sync_implements_relationships",
                "(Repository)-[:IMPLEMENTS]->(Publication)",
                removed,
            )
        return removed

    def _wrap_merge(self, label: str, method_name: str, merges: list[tuple[str, str]]) -> int:
        """Shared wrapper for merge_person/publication/repository_nodes_batch.

        Diffs only the node properties (duplicate -> None = "deleted", canonical's filled-in fields = "updated").
        Relationship rewiring inside _fold_nodes_batch is a mechanical consequence of the merge and isn't diffed
        separately: the node-level entry already tells an operator "id X was folded into id Y at time T by actor Z".
        """
        method = getattr(self._client, method_name)
        if not merges:
            return 0
        ids = list({i for pair in merges for i in pair})
        before = self._fetch_node_props(label, ids)
        removed = method(merges)
        after = self._fetch_node_props(label, ids)
        self._emit_diffs(method_name, label, before, after)
        return removed

    def merge_person_nodes_batch(self, merges: list[tuple[str, str]]) -> int:
        return self._wrap_merge("Person", "merge_person_nodes_batch", merges)

    def merge_publication_nodes_batch(self, merges: list[tuple[str, str]]) -> int:
        return self._wrap_merge("Publication", "merge_publication_nodes_batch", merges)

    def merge_repository_nodes_batch(self, merges: list[tuple[str, str]]) -> int:
        return self._wrap_merge("Repository", "merge_repository_nodes_batch", merges)

    def delete_nodes_batch(self, label: str, ids: list[str], detach: bool = True) -> int:
        # Always retain per-node snapshots: deleted properties cannot be read back.
        if not ids:
            return 0
        before = self._fetch_node_props(label, ids)
        removed = self._client.delete_nodes_batch(label, ids, detach)
        after = self._fetch_node_props(label, ids)
        self._emit_diffs("delete_nodes", label, before, after)
        return removed

    def delete_relationships_batch(
        self,
        src_label: str,
        tgt_label: str,
        rel_type: str,
        pairs: list[tuple[str, str]],
        tgt_match_prop: str = "id",
    ) -> int:
        if not pairs:
            return 0
        entity_type = f"({src_label})-[:{rel_type}]->({tgt_label})"
        before = self._fetch_rel_props(src_label, tgt_label, rel_type, tgt_match_prop, pairs)
        removed = self._client.delete_relationships_batch(
            src_label, tgt_label, rel_type, pairs, tgt_match_prop)
        after = self._fetch_rel_props(src_label, tgt_label, rel_type, tgt_match_prop, pairs)
        self._emit_diffs("delete_relationships", entity_type, before, after)
        return removed

    def promote_link_candidates_batch(self, candidates: list[tuple[str, str]]) -> None:
        if not candidates:
            return
        ids = [c for c, _ in candidates]
        before = self._fetch_node_props("LinkCandidate", ids)
        self._client.promote_link_candidates_batch(candidates)
        after = self._fetch_node_props("LinkCandidate", ids)  # candidates that were promoted vanish -> "deleted"
        self._emit_diffs("promote_link_candidates", "LinkCandidate", before, after)


class SharedGraph:
    """Share one driver pool while giving each caller its own audited wrapper.

    The service owns close(); request wrappers must not close the shared driver.
    """

    def __init__(self, config: Settings, db: Database | None = None, **driver_options):
        self._raw = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password,
                                **driver_options)
        self._sink = build_audit_sink(config, db)

    def audited(self, actor: str | None = None, source: str | None = None) -> AuditedNeo4jClient:
        return _NonClosing(self._raw, self._sink, actor=actor, source=source)

    def close(self) -> None:
        self._raw.close()


class _NonClosing(AuditedNeo4jClient):
    """Keep request cleanup from closing the shared driver pool."""

    def close(self) -> None:
        return None
