"""Rebuilding the map, end to end.

Three steps that were only ever run one after another by hand:

    pauk cache export
    python -m pauk.gui.generate_data
    python -m pauk.gui.generate_stats

Each already had a callable core; this names the sequence, so the
maintenance worker asks for "rebuild the map" instead of assembling three
command lines. Nothing here parses arguments — a value that came from a
form must never become part of a command.

Both the snapshot and the checks read Neo4j, so a rebuild that overlaps a
publish would picture a graph half-written. The caller holds `GRAPH` for
the whole of it; see `pauk.jobs.locks`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pauk.cache import GraphSnapshotExporter
from pauk.gui.generate_data import write_graph_files
from pauk.gui.generate_stats import write_stats
from pauk.settings import Settings

logger = logging.getLogger(__name__)


def rebuild_map(config: Settings, *, public: bool = False, seed: int = 42,
                snapshot_path: Path | None = None) -> dict[str, int]:
    """Export a fresh snapshot and write every file the map is served from.

    Args:
        config: Settings, for the Neo4j connection and where the map lives.
        public: Drop personal fields, for the build that leaves the
            corporate network.
        seed: Layout seed. Held steady between runs on purpose — a new
            layout every rebuild would move a map people navigate by shape.
        snapshot_path: Reuse an existing snapshot instead of exporting one.
            For a rebuild that only changes `public`, where exporting the
            same graph twice is pure waiting.

    Returns:
        Counts from both halves: what the map holds, and what the graph
        holds behind it. Named apart because they are not the same numbers —
        the map leaves out publications with no ITMO author.
    """
    out_dir = config.map_out_dir(public)
    if snapshot_path is None:
        snapshot_path = GraphSnapshotExporter(config).export()
        logger.info("map rebuild: snapshot at %s", snapshot_path)
    counts = write_graph_files(snapshot_path, out_dir, seed=seed, public=public)
    counts.update(write_stats(out_dir))
    logger.info("map rebuild: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts
