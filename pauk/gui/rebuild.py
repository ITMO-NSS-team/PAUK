"""Rebuilding the map: a fresh snapshot, then the site data.

The worker asks for a rebuild instead of assembling the two command lines
(`pauk cache export`, `pauk gui build`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pymongo.database import Database

from pauk.cache import GraphSnapshotExporter
from pauk.gui.graph_builder.builder import write_site_data
from pauk.jobs.locks import held
from pauk.jobs.models import GRAPH
from pauk.settings import Settings

logger = logging.getLogger(__name__)


def rebuild_map(config: Settings, mongo_db: Database, *, seed: int = 42,
                snapshot_path: Path | None = None) -> dict[str, int]:
    """Export a fresh snapshot and write every file the map is served from.

    Holds the graph throughout: the snapshot reads Neo4j, and a concurrent
    publish would leave it half-written. Both builds (`public/` and `private/`
    under `config.gui_dir`) are written.

    Args:
        seed: Layout seed. Held steady between runs, or a rebuild moves a
            map people navigate by shape.
        snapshot_path: Reuse an existing snapshot instead of exporting one.

    Returns:
        What went onto the map, see `write_site_data`.
    """
    with held(mongo_db, GRAPH):
        if snapshot_path is None:
            snapshot_path = GraphSnapshotExporter(config).export()
            logger.info("map rebuild: snapshot at %s", snapshot_path)
        counts = write_site_data(snapshot_path, config.gui_dir, seed)
    logger.info("map rebuild: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts
