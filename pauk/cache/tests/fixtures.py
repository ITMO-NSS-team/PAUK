"""Complete exported columns in small synthetic graph fixtures."""

from pathlib import Path

from pauk.cache.graph_snapshot import write_snapshot
from pauk.cache.snapshot_schema import SNAPSHOT_COLUMNS


def complete_snapshot(graph: dict[str, list]) -> dict[str, list]:
    """Add null properties and empty tables omitted by focused test fixtures."""
    return {
        name: [{**dict.fromkeys(columns), **row} for row in graph.get(name, [])]
        for name, columns in SNAPSHOT_COLUMNS.items()
    }


def write_complete_snapshot(path: Path, graph: dict[str, list]) -> None:
    """Write a full export-shaped fixture from a test's focused rows."""
    write_snapshot(path, complete_snapshot(graph))
