"""CLI entry point for loading pipeline JSONL or shared CSV into Neo4j."""

import argparse
import logging
from pathlib import Path

from pymongo.database import Database

from pauk.settings import Settings, settings
from pauk.storage import PreparedStore

from .client import Neo4jClient
from .csv_loader import load_csv_dir
from .jsonl_loader import load_jsonl_dir, load_prepared_rows
from .schema import create_constraints

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
    client = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
    try:
        create_constraints(client)
        load_prepared_rows(client, rows_by_file)
    finally:
        client.close()


def main() -> None:
    """CLI entry point: `uv run python -m pauk.graph.load`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Load pipeline output into Neo4j.")
    parser.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    parser.add_argument(
        "--dir",
        default=None,
        help="Input directory (default: prepared data for JSONL, data root for CSV)",
    )
    parser.add_argument("--uri", default=settings.neo4j_uri)
    parser.add_argument("--user", default=settings.neo4j_user)
    parser.add_argument("--password", default=settings.neo4j_password)
    args = parser.parse_args()

    client = Neo4jClient(args.uri, args.user, args.password)
    try:
        create_constraints(client)
        if args.format == "jsonl":
            load_jsonl_dir(client, Path(args.dir or settings.prepared_dir))
        else:
            load_csv_dir(client, Path(args.dir or settings.data_dir))
    finally:
        client.close()


if __name__ == "__main__":
    main()
