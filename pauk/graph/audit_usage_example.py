"""Usage examples for AuditedNeo4jClient, not meant to be run as-is.

They show the integration shapes: bulk ETL, a single edit, reading the log back.
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


# 1. ETL load. Batches are large, so most calls hit the bulk-summary path
#    rather than a per-node diff (see diff_threshold); that is intentional.
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
        raw_client.close()


# 2. Single-record edit. A one-row batch is under diff_threshold, so the full
#    field-level diff is logged, e.g. {"email": ("old@x.com", "new@x.com")}.
def update_person_email(client, person_id: str, new_email: str, editor_email: str) -> None:
    """Example of what a `PATCH /persons/{id}` handler would do."""
    with actor_context(f"user:{editor_email}", source="admin-ui"):
        client.upsert_nodes_batch("Person", [(person_id, {"email": new_email})])
        # -> writes exactly one AuditEntry (change_kind="updated",
        #    diff={"email": (old_value, "new@x.com")}) to audit.jsonl,
        #    with actor="user:<editor_email>", source="admin-ui".


# 3. Reading the log back ad hoc (there is no Neo4jAuditSink yet).
def print_recent_changes(entity_id: str, log_path: Path) -> None:
    import json

    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            if entry["entity_id"] == entity_id:
                print(entry["timestamp"], entry["actor"], entry["diff"])

