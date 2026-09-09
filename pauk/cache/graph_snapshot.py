"""Lifecycle of the graph snapshot file: write, read, find on disk."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pauk.storage import AtomicWriter


def write_snapshot(path: Path, graph: dict[str, list]) -> None:
    """Atomically writes a graph snapshot to disk.

    Args:
        path: Where to write the snapshot.
        graph: Flat dict of graph tables - exactly what `export.py::load_db()` returns.

    Example:
        >>> write_snapshot(Path("/tmp/snapshot.json"), {"persons": []})
    """
    with AtomicWriter(path) as fh:
        json.dump(graph, fh, ensure_ascii=False, separators=(",", ":"))


def read_snapshot(path: Path) -> dict[str, list]:
    """Reads a snapshot written by `write_snapshot`.

    Args:
        path: Path to the snapshot file.

    Returns:
        The graph dict - the same tables passed to `write_snapshot`.

    Raises:
        ValueError: the file's top-level JSON isn't an object.

    Example:
        >>> read_snapshot(Path("/tmp/snapshot.json"))
        {'persons': []}
    """
    graph: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(graph, dict):
        raise ValueError("graph snapshot is not a JSON object")
    return graph


def dated_snapshot_path(cache_dir: Path) -> Path:
    """Builds today's snapshot path.

    Each export run gets its own file - a second `pauk cache export` on the
    same day overwrites that day's file, not yesterday's.

    Args:
        cache_dir: Snapshot directory (usually `Settings.cache_dir`).

    Returns:
        `<cache_dir>/graph_snapshot_<dd-mm-yyyy>.json`.

    Example:
        >>> dated_snapshot_path(Path("/data/cache"))
        PosixPath('/data/cache/graph_snapshot_10-09-2026.json')
    """
    return cache_dir / f"graph_snapshot_{date.today():%d-%m-%Y}.json"


def latest_snapshot(cache_dir: Path) -> Path:
    """Finds the snapshot whose filename date is the latest.

    Files that don't match `graph_snapshot_<dd-mm-yyyy>.json` are ignored.

    Args:
        cache_dir: Snapshot directory.

    Returns:
        The `graph_snapshot_<dd-mm-yyyy>.json` file with the latest date.

    Raises:
        FileNotFoundError: no matching file in the directory.
    """
    dated: dict[date, Path] = {}
    for path in cache_dir.glob("graph_snapshot_*.json"):
        try:
            dated[datetime.strptime(path.stem, "graph_snapshot_%d-%m-%Y").date()] = path
        except ValueError:
            continue
    if not dated:
        raise FileNotFoundError(f"no snapshot found in {cache_dir} - run 'pauk cache export' first")
    return dated[max(dated)]
