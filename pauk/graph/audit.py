"""Audit-logging wrapper around Neo4jClient.

Wraps every mutating Neo4jClient method: snapshots the affected entities before and after the underlying call,
computes a field-level diff, and hands the resulting AuditEntry batch to an AuditSink. Read-only methods
(fetch_*, close, ...) pass straight through untouched.

Usage
-----
    client = Neo4jClient(uri, user, password)
    audited = AuditedNeo4jClient(client, JSONLAuditSink(Path("audit.jsonl")))

    # ETL / bulk load — cheap summary entries, no per-node diff:
    with actor_context("etl-pipeline", source="jsonl_loader"):
        load_prepared_rows(audited, rows_by_file)

    # Future front-end — single-entity mutation, full diff:
    with actor_context(f"user:{current_user.email}", source="admin-ui"):
        audited.upsert_nodes_batch("Person", [(person_id, {"email": new_email})])

Failure behaviour
-----------------
If the wrapped Neo4jClient call raises, the exception propagates before any AuditEntry is built or written — a failed
write never produces an audit record, so the log never claims a change that didn't happen. Because jsonl_loader.py
chunks its own batches and calls each chunk through this wrapper separately, a failure partway through a load leaves
the already-processed chunks consistent between Neo4j and the audit log (each chunk's Neo4j commit and its audit
write both happened, or neither did). The one gap this can't close: the audit write happens *after* the Neo4j
transaction commits, as a separate step — a crash in that narrow window leaves the graph updated with no matching
audit entry. Closing that gap needs the audit write inside the same Neo4j transaction as the data write (e.g. a
future Neo4jAuditSink writing :AuditEvent nodes as part of the same execute_write call) — out of scope for the JSONL
sink.

Design notes
------------
- `actor`/`source` travel via contextvars rather than as an explicit argument to every call, so `jsonl_loader.py` and
  any future CRUD code don't need to thread an actor through every function signature — only the outermost caller
  sets it, once, for the whole operation.
- Batches at or above `diff_threshold` get a single coarse "bulk_write" entry (counts only) instead of one AuditEntry
  per row: diffing every node in a 2000-row ETL chunk would double the query count for very little audit value. Small
  batches (typical of a front-end edit) get a full before/after field diff.
- AuditSink is a narrow protocol so the storage backend (JSONL file today, Neo4j :AuditEvent nodes or Postgres later)
  can change without touching AuditedNeo4jClient or any caller.
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
    """Append-only JSONL sink — one line per AuditEntry. Base implementation.

    Good enough to start with and to grep/tail during development. Swap for
    a Neo4jAuditSink (writing :AuditEvent nodes linked to the changed
    entity) or a Postgres sink later — nothing outside this module needs
    to change, AuditedNeo4jClient only knows about the AuditSink protocol.
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
    """A value MongoDB can store, or its string form.

    Property values come back from the driver as whatever type Neo4j used
    (neo4j.time.DateTime among them), and pymongo refuses what it cannot
    encode. The audit path must never be the thing that fails a write, so
    anything unrecognised is kept as text — same reasoning as `default=str`
    in the JSONL sink.
    """
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_storable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _storable(item) for key, item in value.items()}
    return str(value)


class MongoAuditSink:
    """Audit entries in a MongoDB collection, for the panel's change feed.

    JSONL stays useful for grepping a run; a feed in the UI needs filters
    by actor and entity plus pagination, which a flat file cannot serve.
    Both sinks can run side by side — AuditedNeo4jClient takes one sink, so
    pair them with `MultiAuditSink` when both are wanted.
    """

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
    """Fans one batch of entries out to several sinks, in order.

    A sink that raises stops the rest: losing an audit record silently is
    worse than a loud failure, and the caller already treats an audit
    error as a failed write.
    """

    def __init__(self, *sinks: AuditSink) -> None:
        self.sinks = sinks

    def write(self, entries: Sequence[AuditEntry]) -> None:
        for sink in self.sinks:
            sink.write(entries)


def build_audit_sink(config: Settings, db: Database | None = None) -> AuditSink:
    """The sink every entry point should use.

    JSONL stays for grepping a run from the shell; the Mongo collection is
    what the panel's change feed can filter and paginate. Without a
    database only the file is written, which keeps the CLI usable when
    Mongo is not at hand.
    """
    config.audit_dir.mkdir(parents=True, exist_ok=True)
    jsonl = JSONLAuditSink(config.audit_dir / "audit.jsonl")
    return MultiAuditSink(jsonl, MongoAuditSink(db)) if db is not None else jsonl


def audited_client(config: Settings, db: Database | None = None,
                   **driver_options) -> AuditedNeo4jClient:
    """An open Neo4j client that records what it changes.

    Callers own the returned client and must close() it, same as with a
    plain Neo4jClient. Wrap the actual work in `actor_context` so the
    entries say who made the change.
    """
    raw = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password,
                      **driver_options)
    return AuditedNeo4jClient(raw, build_audit_sink(config, db))


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

    def __init__(self, client: Neo4jClient, sink: AuditSink, diff_threshold: int = 50):
        """
        Args:
            client: The real Neo4jClient to wrap.
            sink: Where audit entries go.
            diff_threshold: Batches with this many rows or more get one coarse summary entry instead of a per-row
                diff. Keeps large ETL loads cheap while front-end single-row edits still get full field-level diffs.
        """
        self._client = client
        self._sink = sink
        self._diff_threshold = diff_threshold

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    # -- shared plumbing ---------------------------------------------------
    
    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _emit_bulk_summary(self, operation: str, entity_type: str, row_count: int) -> None:
        self._sink.write(
            [
                AuditEntry(
                    timestamp=self._now(),
                    actor=_actor_var.get(),
                    source=_source_var.get(),
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
        actor, source = _actor_var.get(), _source_var.get()
        entries = []
        for entity_id in before_by_id.keys() | after_by_id.keys():
            diff, kind = _diff_props(before_by_id.get(entity_id), after_by_id.get(entity_id))
            if not diff:
                continue
            entries.append(AuditEntry(now, actor, source, operation, entity_type, entity_id, kind, diff))
        self._sink.write(entries)

    def _fetch_node_props(self, labels: str | list[str], ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        label_list = labels if isinstance(labels, list) else [labels]
        # Match a node carrying ANY of the given labels, not all of them: upsert_nodes_batch can add a label
        # a node doesn't have yet (e.g. "Person" -> "Person:Itmo"), and the "before" snapshot must still find
        # the node by whichever label(s) it already carries — otherwise a real update on a node that's about
        # to gain a label looks like "created" and the old field values are lost from the audit log.
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

    # -- wrapped write methods ----------------------------------------------

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

    def upsert_person_nodes_batch(self, nodes: list[tuple[str, dict]], is_itmo: bool):
        if not nodes:
            return
        if len(nodes) >= self._diff_threshold:
            self._client.upsert_person_nodes_batch(nodes, is_itmo)
            self._emit_bulk_summary("upsert_person_nodes", "Person", len(nodes))
            return
        ids = [node_id for node_id, _ in nodes]
        before = self._fetch_node_props("Person", ids)
        self._client.upsert_person_nodes_batch(nodes, is_itmo)
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

    def _wrap_merge(self, label: str, method_name: str, merges: list[tuple[str, str]]) -> int:
        """Shared wrapper for merge_person/publication/repository_nodes_batch.

        Diffs only the node properties (duplicate -> None = "deleted", canonical's filled-in fields = "updated").
        Relationship rewiring inside _fold_nodes_batch is a mechanical consequence of the merge and isn't diffed
        separately — the node-level entry already tells an operator "id X was folded into id Y at time T by actor Z".
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
        # Always diffed per node, whatever the batch size: a deletion is the
        # one change that cannot be reconstructed from the graph afterwards,
        # so the entry carrying the node's last known fields is the only
        # record left of what was there.
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
