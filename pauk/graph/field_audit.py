"""Offline audit of identity and display fields in saved graph tables.

Run with ``python -m pauk.graph.field_audit snapshot.json``. This command
reads one file and prints evidence; it never connects to or modifies a database.
Missing projection columns are reported separately from empty stored values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

FIELD_GROUPS = {
    "repositories": (("id",), ("name",), ("url",)),
    "publications": (("id",), ("title",)),
    "persons": (("id",), ("name_raw", "name_ru", "name_en", "surname_ru", "surname_en")),
    "departments": (("id",), ("name_ru", "name_en")),
    "organizations": (("id",), ("name_ru", "name_en")),
    "github_profiles": (("id",), ("login",)),
    "link_candidates": (("id",), ("url",)),
}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def audit_snapshot(snapshot: dict[str, Any], *, complete_properties: bool = False) -> dict[str, Any]:
    """Report missing identity/display values without inventing replacements.

    Alternative name fields form one requirement: a missing translation is
    not a missing identity. Optional descriptions, emails and metrics are
    intentionally excluded. A projected graph cannot establish whether an
    unexported field is missing in Neo4j.
    """
    findings = []
    coverage = {}
    for table, requirements in FIELD_GROUPS.items():
        if table not in snapshot:
            coverage[table] = {"status": "not_exported"}
            continue
        rows = snapshot[table]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{table} must be a list of objects")
        counts: Counter[str] = Counter()
        for index, row in enumerate(rows):
            for fields in requirements:
                present = [field for field in fields if field in row]
                placeholder = (
                    table == "publications" and fields == ("title",)
                    and isinstance(row.get("title"), str) and row["title"].strip().casefold() == "untitled"
                )
                if any(_text(row[field]) for field in present) and not placeholder:
                    continue
                if placeholder:
                    status = "placeholder"
                elif not present:
                    status = "missing" if complete_properties else "not_exported"
                elif not complete_properties and len(present) < len(fields):
                    status = "incomplete_projection"
                elif any(row[field] is not None and not isinstance(row[field], str) for field in present):
                    status = "invalid_type"
                else:
                    status = "empty"
                counts[status] += 1
                findings.append({
                    "table": table,
                    "row_index": index,
                    "id": row.get("id"),
                    "fields": list(fields),
                    "status": status,
                    "observed": {field: row[field] for field in present},
                    "url": row.get("url"),
                    "next_check": "compare the same node with graph properties and its prepared/source record",
                })
        coverage[table] = {"status": "checked", "rows": len(rows), "findings": dict(counts)}
    display_findings = []
    display_fields = ("name_ru", "name_en", "surname_ru", "surname_en")
    for row in snapshot.get("persons", []):
        if any(_text(row.get(field)) for field in display_fields):
            continue
        if not complete_properties and not all(field in row for field in display_fields):
            continue
        display_findings.append({
            "table": "persons",
            "id": row.get("id"),
            "status": "missing_display_name",
            "fields": list(display_fields),
            "raw_name_available": _text(row.get("name_raw")),
            "next_check": "name_raw alone is not a GUI label; check prepared name enrichment",
        })
    return {"coverage": coverage, "findings": findings,
            "display_findings": display_findings, "writes": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--complete-properties", action="store_true",
                        help="Input contains all stored node properties, not a cache projection")
    args = parser.parse_args()
    content = args.snapshot.read_bytes()
    snapshot = json.loads(content.decode("utf-8-sig"))
    if not isinstance(snapshot, dict):
        parser.error("snapshot must be a JSON object of graph tables")
    report = audit_snapshot(snapshot, complete_properties=args.complete_properties)
    report["source"] = {"path": str(args.snapshot.resolve()), "sha256": hashlib.sha256(content).hexdigest()}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
