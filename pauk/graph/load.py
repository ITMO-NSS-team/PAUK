"""CLI entry point for loading pipeline JSONL or shared CSV into Neo4j."""

import argparse
import logging
from pathlib import Path

from pauk.settings import Settings, settings

from .client import Neo4jClient
from .csv_loader import load_csv_dir
from .jsonl_loader import load_jsonl_dir
from .schema import create_constraints


def load_jsonl_group(config: Settings, group: str) -> None:
    """Load one prepared-JSONL group into Neo4j. Used by `pauk publish graph`.

    Args:
        config: Application settings (Neo4j connection, data directories).
        group: Name of the group directory under config.prepared_dir.
    """
    client = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
    try:
        create_constraints(client)
        load_jsonl_dir(client, config.prepared_dir / group)
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
