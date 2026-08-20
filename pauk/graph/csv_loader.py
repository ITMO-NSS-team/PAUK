"""Generic CSV loader for nodes/relationships, independent of the JSONL
pipeline path. Node files use columns id/labels/properties, relationship
files use start_id/end_id/src_label/tgt_label/type/properties. Nothing in
this repository currently produces such CSV files — this path exists for
external/manual data loads and stays ready for when one shows up.
"""

import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

from .client import CHUNK_SIZE, Neo4jClient

logger = logging.getLogger(__name__)


def load_nodes_from_csv(client: Neo4jClient, csv_path: str, batch_size: int = CHUNK_SIZE):
    """Load nodes from a CSV file into Neo4j, in batches.

    Args:
        client: An open Neo4jClient to load data into.
        csv_path: Path to a CSV file with columns id, labels, properties
            (properties is a JSON string).
        batch_size: Maximum number of nodes per Neo4j write.
    """
    batches_by_labels = defaultdict(list)

    with open(csv_path, encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            node_id = row["id"]
            labels = row["labels"].strip()
            properties_str = row.get("properties", "{}")
            properties = json.loads(properties_str) if properties_str else {}

            batches_by_labels[labels].append((node_id, properties))

            if len(batches_by_labels[labels]) >= batch_size:
                client.upsert_nodes_batch(labels, batches_by_labels[labels])
                batches_by_labels[labels].clear()

    for labels, batch in batches_by_labels.items():
        if batch:
            client.upsert_nodes_batch(labels, batch)


def load_relationships_from_csv(client: Neo4jClient, csv_path: str, batch_size: int = CHUNK_SIZE):
    """Load relationships from a CSV file into Neo4j, in batches.

    Args:
        client: An open Neo4jClient to load data into.
        csv_path: Path to a CSV file with columns start_id, end_id,
            src_label, tgt_label, type, properties (properties is a JSON
            string).
        batch_size: Maximum number of relationships per Neo4j write.
    """
    batches_by_rel = defaultdict(list)

    with open(csv_path, encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            src_id = row["start_id"]
            tgt_id = row["end_id"]
            src_label = row["src_label"].strip()
            tgt_label = row["tgt_label"].strip()
            rel_type = row["type"].strip()
            properties_str = row.get("properties", "{}")
            properties = json.loads(properties_str) if properties_str else {}

            key = (src_label, tgt_label, rel_type)
            batches_by_rel[key].append((src_id, tgt_id, properties))

            if len(batches_by_rel[key]) >= batch_size:
                client.upsert_relationships_batch(src_label, tgt_label, rel_type, batches_by_rel[key])
                batches_by_rel[key].clear()

    for (src_label, tgt_label, rel_type), batch in batches_by_rel.items():
        if batch:
            client.upsert_relationships_batch(src_label, tgt_label, rel_type, batch)


def discover_files(data_dir: Path, suffix: str) -> list[Path]:
    """Return all files under `data_dir` whose name ends with `suffix`, sorted."""
    return sorted(data_dir.glob(f"*{suffix}"))


def load_csv_dir(client: Neo4jClient, data_dir: Path) -> None:
    """Load every *_nodes.csv file, then every *_rels.csv file, from a directory.

    Same nodes-before-relationships ordering as jsonl_loader.load_prepared_rows.

    Args:
        client: An open Neo4jClient to load data into.
        data_dir: Directory to scan for *_nodes.csv/*_rels.csv files.
    """
    if not data_dir.exists():
        logger.error("Data directory does not exist: %s", data_dir)
        return

    node_files = discover_files(data_dir, "_nodes.csv")
    rel_files = discover_files(data_dir, "_rels.csv")
    logger.info("Found %d node file(s), %d relationship file(s)", len(node_files), len(rel_files))

    for file_path in node_files:
        logger.info("Loading nodes: %s", file_path.name)
        try:
            load_nodes_from_csv(client, str(file_path))
        except Exception:
            logger.error("Failed to load node file %s", file_path.name, exc_info=True)

    for file_path in rel_files:
        logger.info("Loading relationships: %s", file_path.name)
        try:
            load_relationships_from_csv(client, str(file_path))
        except Exception:
            logger.error("Failed to load relationship file %s", file_path.name, exc_info=True)
