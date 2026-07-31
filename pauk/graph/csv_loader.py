""" /   CSV ( id/labels/properties,
start_id/end_id/src_label/tgt_label/type/properties) —   
 scripts/data_to_graph.py,   ,   
 (: `from neo4j_client import ...` —    
, . neo4j-connector.md  Obsidian).
"""

import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

from .client import Neo4jClient, CHUNK_SIZE

logger = logging.getLogger(__name__)


def load_nodes_from_csv(client: Neo4jClient, csv_path: str, batch_size: int = CHUNK_SIZE):
    """: id, labels, properties (properties — JSON-)."""
    batches_by_labels = defaultdict(list)

    with open(csv_path, mode="r", encoding="utf-8") as file:
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
    """: start_id, end_id, src_label, tgt_label, type, properties."""
    batches_by_rel = defaultdict(list)

    with open(csv_path, mode="r", encoding="utf-8") as file:
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
    return sorted(data_dir.glob(f"*{suffix}"))


def load_csv_dir(client: Neo4jClient, data_dir: Path) -> None:
    """ (*_nodes.csv),   (*_rels.csv) —   ,  
     jsonl_loader.load_jsonl_dir."""
    if not data_dir.exists():
        logger.error("    : %s", data_dir)
        return

    node_files = discover_files(data_dir, "_nodes.csv")
    rel_files = discover_files(data_dir, "_rels.csv")
    logger.info("  : %d,  : %d", len(node_files), len(rel_files))

    for file_path in node_files:
        logger.info(": %s", file_path.name)
        try:
            load_nodes_from_csv(client, str(file_path))
        except Exception:
            logger.error("     %s", file_path.name, exc_info=True)

    for file_path in rel_files:
        logger.info(": %s", file_path.name)
        try:
            load_relationships_from_csv(client, str(file_path))
        except Exception:
            logger.error("     %s", file_path.name, exc_info=True)
