"""Точка входа: загрузить выход пайплайна (JSONL) или общий CSV в Neo4j.

Запуск: uv run python -m pauk.graph.load
"""

import argparse
import logging
from pathlib import Path

from data_enrichment.config import DATA_DIR, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

from .client import Neo4jClient
from .csv_loader import load_csv_dir
from .jsonl_loader import load_jsonl_dir
from .schema import create_constraints


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Загрузить выход пайплайна в Neo4j.")
    parser.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    parser.add_argument(
        "--dir", default=None,
        help="Входная директория (по умолчанию: DATA_DIR/enriched для jsonl, DATA_DIR для csv)",
    )
    parser.add_argument("--uri", default=NEO4J_URI)
    parser.add_argument("--user", default=NEO4J_USER)
    parser.add_argument("--password", default=NEO4J_PASSWORD)
    args = parser.parse_args()

    client = Neo4jClient(args.uri, args.user, args.password)
    try:
        create_constraints(client)
        if args.format == "jsonl":
            load_jsonl_dir(client, Path(args.dir or DATA_DIR / "enriched"))
        else:
            load_csv_dir(client, Path(args.dir or DATA_DIR))
    finally:
        client.close()


if __name__ == "__main__":
    main()
