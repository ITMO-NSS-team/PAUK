"""Read-only view of a graph snapshot (`graph_snapshot.py`), without parsing
the raw JSON by hand: `write_snapshot()` writes one line, no indent, to keep
the file small - not meant to be opened in a text editor. Fields in
`pauk/graph/extract.py::JSON_TEXT_FIELDS` (`funding`/`versions`/
`counts_by_year`/`affiliations`) are stored as an already-serialized JSON
string (Neo4j can't hold nested map/list-of-map) - decoded back into an
object here before printing.

CLI: `pauk cache inspect` (`pauk/cli.py`). This module only exposes the
display functions (`summarize`/`describe_table`/`sample_rows`), no argparse
of its own.
"""

from __future__ import annotations

import json
from typing import Any

from pauk.graph.extract import JSON_TEXT_FIELDS


def _decode_json_text(field: str, value: Any) -> Any:
    """Decodes a JSON-text field back into an object for readable printing.

    Args:
        field: Field name - the decode/leave-alone decision is made by
            name, not content (see JSON_TEXT_FIELDS).
        value: The field's value from the snapshot.

    Returns:
        The parsed object if `field` is in JSON_TEXT_FIELDS and `value` is
        valid JSON. Otherwise `value` unchanged - None and broken JSON
        included, this must not raise.
    """
    if field not in JSON_TEXT_FIELDS or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def summarize(graph: dict[str, list]) -> None:
    """Prints each table's name and row count, column-aligned. Field names
    are shown per-table instead (see describe_table) - a full field list
    here wouldn't fit one line for a wide table like persons.
    """
    if not graph:
        return
    name_width = max(len(name) for name in graph)
    count_width = max(len(str(len(rows))) for rows in graph.values())
    for name, rows in graph.items():
        print(f"{name:<{name_width}}  {len(rows):>{count_width}} rows")


def describe_table(rows: list[dict], table: str) -> None:
    """Prints each field's null count, type, and whether it's JSON-text.
    Sorted by null percentage descending - the sparsest fields first.
    """
    if not rows:
        print(f"{table}: no rows")
        return

    stats = []
    for field in rows[0]:
        values = [row.get(field) for row in rows]
        non_null = [v for v in values if v is not None]
        n_null = len(values) - len(non_null)
        types = ", ".join(sorted({type(v).__name__ for v in non_null})) or "-"
        if field in JSON_TEXT_FIELDS:
            types += " [json]"
        stats.append((field, n_null, 100 * n_null / len(values), types))
    stats.sort(key=lambda row: row[1], reverse=True)

    name_width = max(len(field) for field, *_ in stats)
    for field, n_null, pct_null, types in stats:
        print(f"{field:<{name_width}}  null={n_null:>6} ({pct_null:5.1f}%)  {types}")


def sample_rows(rows: list[dict], n: int) -> None:
    """Prints the first `n` rows, numbered, with JSON-text fields decoded
    (see _decode_json_text) instead of shown as escaped strings.
    """
    total = min(n, len(rows))
    for i, row in enumerate(rows[:n], start=1):
        decoded = {field: _decode_json_text(field, value) for field, value in row.items()}
        print(f"── {i}/{total} ──")
        print(json.dumps(decoded, ensure_ascii=False, indent=2))
