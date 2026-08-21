"""Usage examples for AuditedNeo4jClient — not meant to be run as-is,
just the two integration shapes: bulk ETL and a single front-end edit.
"""

from pathlib import Path

from pymongo.database import Database

from pauk.graph.audit import AuditedNeo4jClient, JSONLAuditSink, actor_context
from pauk.graph.client import Neo4jClient
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.schema import create_constraints
from pauk.settings import Settings
from pauk.storage import PreparedStore


# ---------------------------------------------------------------------
# 1. ETL load (this replaces load_jsonl_group in load.py). Batches are large here, so most calls will hit the
#    bulk-summary path rather than a per-node diff (see diff_threshold) — that's intentional.
# ---------------------------------------------------------------------
def load_jsonl_group_audited(config: Settings, mongo_db: Database, group: str) -> None:
    prepared = PreparedStore(mongo_db, group)
    rows_by_file = {filename: list(prepared.read_rows(entity)) for entity, filename in ENTITY_FILES.items()}
    raw_client = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
    sink = JSONLAuditSink(config.cache_dir / "audit.jsonl")
    client = AuditedNeo4jClient(raw_client, sink)
    try:
        create_constraints(client)  # passes through untouched, DDL isn't audited
        with actor_context("etl-pipeline", source=f"prepared_rows:{group}"):
            load_prepared_rows(client, rows_by_file)
    finally:
        raw_client.close()  # close() also passes through __getattr__; either
        # raw_client.close() or client.close() works, they're the same object


# ---------------------------------------------------------------------
# 2. Single-record edit from a future admin UI / API. Small batch (one row) -> under diff_threshold -> full field-level
#    diff gets logged, e.g. {"email": ("old@x.com", "new@x.com")}.
# ---------------------------------------------------------------------
def update_person_email(client, person_id: str, new_email: str, editor_email: str) -> None:
    """Example of what a future `PATCH /persons/{id}` handler would do."""
    with actor_context(f"user:{editor_email}", source="admin-ui"):
        client.upsert_nodes_batch("Person", [(person_id, {"email": new_email})])
        # -> writes exactly one AuditEntry (change_kind="updated",
        #    diff={"email": (old_value, "new@x.com")}) to audit.jsonl,
        #    with actor="user:<editor_email>", source="admin-ui".


# ---------------------------------------------------------------------
# 3. Reading the log back (ad-hoc, while there's no Neo4jAuditSink yet).
# ---------------------------------------------------------------------
def print_recent_changes(entity_id: str, log_path: Path) -> None:
    import json

    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            if entry["entity_id"] == entity_id:
                print(entry["timestamp"], entry["actor"], entry["diff"])

