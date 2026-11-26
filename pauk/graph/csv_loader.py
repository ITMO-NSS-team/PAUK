"""Import external/manual CSV nodes and relationships with row diagnostics."""

import csv
import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .client import CHUNK_SIZE, Neo4jClient
from .validation import CsvNodeError

logger = logging.getLogger(__name__)


@dataclass
class CsvImportReport:
    """Counts refer to input rows, including updates of existing graph records."""

    nodes_loaded: int = 0
    nodes_skipped: int = 0
    relationships_loaded: int = 0
    relationships_skipped: int = 0
    issues: list[dict] = field(default_factory=list)

    def reject(self, kind: str, path: str, line: int, row_id: str, reason: str) -> None:
        if kind == "node":
            self.nodes_skipped += 1
        else:
            self.relationships_skipped += 1
        self.issue(path, line, row_id, reason)

    def issue(self, path: str, line: int, row_id: str, reason: str) -> None:
        self.issues.append({"file": str(path), "line": line, "id": row_id, "reason": reason})
        logger.warning("%s:%s: id=%s: %s", path, line, row_id, reason)


def _properties(row: dict) -> dict:
    raw = row.get("properties", "{}")
    properties = json.loads(raw) if raw else {}
    if not isinstance(properties, dict):
        raise ValueError("properties must be a JSON object")
    for name, value in properties.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is None and not isinstance(value, list):
                continue
            if type(item) not in (str, bool, int, float):
                raise ValueError(f"property {name!r} must be a scalar or a flat list without nulls")
            if type(item) is int and not -(2 ** 63) <= item < 2 ** 63:
                raise ValueError(f"property {name!r} contains an integer outside signed 64-bit range")
            if type(item) is float and not math.isfinite(item):
                raise ValueError(f"property {name!r} contains a non-finite number")
        if isinstance(value, list):
            types = {type(item) for item in value}
            if len(types) > 1 and not types <= {int, float}:
                raise ValueError(f"property {name!r} must contain values of one compatible type")
    return properties


def _required(row: dict, field_name: str) -> str:
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    return value


def _labels(row: dict, field_name: str) -> str:
    labels = [label.strip() for label in _required(row, field_name).split(":")]
    if not all(label.isidentifier() for label in labels):
        raise ValueError(f"{field_name} must contain colon-separated identifiers")
    return ":".join(labels)


def load_nodes_from_csv(client: Neo4jClient, csv_path: str, batch_size: int = CHUNK_SIZE,
                        report: CsvImportReport | None = None) -> CsvImportReport:
    """Skip malformed rows; preserve omitted properties on valid partial updates.

    Repository rows are applied in order, with their resulting name validated
    transactionally. Other labels retain the generic batch import path.
    Database failures propagate: an unavailable graph is not a malformed row.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    report = report if report is not None else CsvImportReport()
    batches_by_labels = defaultdict(list)

    def flush(labels):
        batch = batches_by_labels[labels]
        if batch:
            client.upsert_nodes_batch(labels, batch)
            report.nodes_loaded += len(batch)
            batch.clear()

    with open(csv_path, encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            line = reader.line_num
            try:
                node_id = _required(row, "id")
                labels = _labels(row, "labels")
                properties = _properties(row)
            except ValueError as exc:
                report.reject("node", csv_path, line, row.get("id") or "", str(exc))
                continue

            if "Repository" in labels.split(":") or "name" in properties:
                # Earlier generic rows may have created this same multilabel node.
                for pending_labels in batches_by_labels:
                    flush(pending_labels)
                try:
                    client.upsert_csv_node(labels, node_id, properties)
                except CsvNodeError as exc:
                    report.reject("node", csv_path, line, node_id, str(exc))
                    continue
                report.nodes_loaded += 1
            else:
                for pending_labels in batches_by_labels:
                    if pending_labels != labels:
                        flush(pending_labels)
                if any(previous_id == node_id for previous_id, _ in batches_by_labels[labels]):
                    flush(labels)
                batches_by_labels[labels].append((node_id, properties))
                if len(batches_by_labels[labels]) >= batch_size:
                    flush(labels)

    for labels in batches_by_labels:
        flush(labels)
    return report


def load_relationships_from_csv(client: Neo4jClient, csv_path: str, batch_size: int = CHUNK_SIZE,
                                report: CsvImportReport | None = None) -> CsvImportReport:
    """Import valid edges and report unmatched endpoints separately from JSON errors."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    report = report if report is not None else CsvImportReport()
    batches_by_rel = defaultdict(list)

    def flush(key):
        batch = batches_by_rel[key]
        if not batch:
            return
        src_label, tgt_label, rel_type = key
        loaded = client.upsert_relationships_batch(
            src_label, tgt_label, rel_type, [item for _, item in batch])
        report.relationships_loaded += loaded
        missing = len(batch) - loaded
        if missing:
            report.relationships_skipped += missing
            report.issue(csv_path, batch[0][0], "",
                         f"{missing} relationship row(s) have missing endpoints in "
                         f"{src_label}-[{rel_type}]->{tgt_label} batch, lines "
                         f"{batch[0][0]}-{batch[-1][0]}")
        batch.clear()

    with open(csv_path, encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            line = reader.line_num
            try:
                src_id = _required(row, "start_id")
                tgt_id = _required(row, "end_id")
                src_label = _labels(row, "src_label")
                tgt_label = _labels(row, "tgt_label")
                rel_type = _required(row, "type").strip()
                if not rel_type.isidentifier():
                    raise ValueError("type must be an identifier")
                properties = _properties(row)
            except ValueError as exc:
                row_id = f"{row.get('start_id') or ''}->{row.get('end_id') or ''}"
                report.reject("relationship", csv_path, line, row_id, str(exc))
                continue
            key = (src_label, tgt_label, rel_type)
            for pending_key in batches_by_rel:
                if pending_key != key:
                    flush(pending_key)
            batches_by_rel[key].append((line, (src_id, tgt_id, properties)))
            if len(batches_by_rel[key]) >= batch_size:
                flush(key)

    for key in batches_by_rel:
        flush(key)
    return report


def discover_files(data_dir: Path, suffix: str) -> list[Path]:
    """Return all files under `data_dir` whose name ends with `suffix`, sorted."""
    return sorted(data_dir.glob(f"*{suffix}"))


def load_csv_dir(client: Neo4jClient, data_dir: Path) -> CsvImportReport:
    """Load node files before relationship files, retaining a combined row report."""
    if not data_dir.is_dir():
        raise NotADirectoryError(data_dir)
    report = CsvImportReport()
    node_files = discover_files(data_dir, "_nodes.csv")
    rel_files = discover_files(data_dir, "_rels.csv")
    logger.info("Found %d node file(s), %d relationship file(s)", len(node_files), len(rel_files))
    for file_path in node_files:
        logger.info("Loading nodes: %s", file_path.name)
        load_nodes_from_csv(client, str(file_path), report=report)
    for file_path in rel_files:
        logger.info("Loading relationships: %s", file_path.name)
        load_relationships_from_csv(client, str(file_path), report=report)
    logger.info("CSV import: nodes loaded=%d skipped=%d; relationships loaded=%d skipped=%d",
                report.nodes_loaded, report.nodes_skipped,
                report.relationships_loaded, report.relationships_skipped)
    return report
