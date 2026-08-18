"""CLI entry point for loading a shared CSV export into Neo4j."""

import argparse
import logging
from pathlib import Path

from pymongo.database import Database

from pauk.settings import Settings, settings
from pauk.storage import PreparedStore

from .audit import actor_context, audited_client
from .client import Neo4jClient
from .csv_loader import load_csv_dir
from .jsonl_loader import load_prepared_rows
from .overrides import apply_overrides, tombstoned_ids
from .schema import create_constraints

logger = logging.getLogger(__name__)

# PreparedStore.COLLECTIONS keys -> the filenames load_prepared_rows expects.
ENTITY_FILES = {
    "organizations": "organizations.jsonl",
    "departments": "departments.jsonl",
    "publications": "publications.jsonl",
    "repositories": "repositories.jsonl",
    "github_profiles": "github_profiles.jsonl",
    "persons": "persons.jsonl",
    "repo_links": "repo_links.jsonl",
}


# Which node label the rows of each prepared entity become. persons.jsonl
# feeds two registry entries (ITMO and external) that share one label.
FILE_LABELS = {
    "departments.jsonl": "Department",
    "publications.jsonl": "Publication",
    "repositories.jsonl": "Repository",
    "github_profiles.jsonl": "GitHubProfile",
    "persons.jsonl": "Person",
}


def _drop_tombstoned(rows_by_file: dict[str, list[dict]], mongo_db: Database) -> dict[str, list[dict]]:
    """Remove rows whose node was deleted by hand.

    Filtering here rather than deleting after the load is what keeps the
    audit log honest: MERGE would recreate the node and apply_overrides
    would remove it again, writing a creation and a deletion nobody asked
    for on every single run.
    """
    filtered = {}
    for filename, rows in rows_by_file.items():
        label = FILE_LABELS.get(filename)
        tombstones = tombstoned_ids(mongo_db, label) if label else set()
        if not tombstones:
            filtered[filename] = rows
            continue
        kept = [row for row in rows if row.get("id") not in tombstones]
        logger.info("publish: %d row(s) in %s skipped as deleted by hand",
                    len(rows) - len(kept), filename)
        filtered[filename] = kept
    return filtered


def load_jsonl_group(config: Settings, mongo_db: Database, group: str) -> None:
    """Load one prepared group from Mongo into Neo4j. Used by `pauk publish graph`.

    Args:
        config: Application settings (Neo4j connection).
        mongo_db: The raw/prepared MongoDB database.
        group: The group whose prepared rows to publish.
    """
    prepared = PreparedStore(mongo_db, group)
    rows_by_file = {
        filename: list(prepared.read_rows(entity))
        for entity, filename in ENTITY_FILES.items()
    }
    rows_by_file = _drop_tombstoned(rows_by_file, mongo_db)
    client = audited_client(config, mongo_db)
    try:
        create_constraints(client)
        # Large batches are recorded as one summary entry each (see
        # AuditedNeo4jClient.diff_threshold), so a publish costs a handful
        # of audit rows, not one per node — but "who republished this group
        # and when" stops being invisible.
        with actor_context("etl-pipeline", source=f"publish:{group}"):
            load_prepared_rows(client, rows_by_file)
            # Last step, after candidate promotion and every fold: publishing
            # overwrites hand-corrected fields with whatever the source says,
            # so the manual decisions are put back on top.
            apply_overrides(client, mongo_db)
    finally:
        client.close()


def main() -> None:
    """CLI entry point: `uv run python -m pauk.graph.load`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Load a shared CSV export into Neo4j.")
    parser.add_argument("--dir", default=None, help="Input directory (default: data root)")
    parser.add_argument("--uri", default=settings.neo4j_uri)
    parser.add_argument("--user", default=settings.neo4j_user)
    parser.add_argument("--password", default=settings.neo4j_password)
    args = parser.parse_args()

    client = Neo4jClient(args.uri, args.user, args.password)
    try:
        create_constraints(client)
        load_csv_dir(client, Path(args.dir or settings.data_dir))
    finally:
        client.close()


if __name__ == "__main__":
    main()
