"""Rebuilding the map: a fresh snapshot, then the site data.

Two steps that were only ever run one after another by hand (`pauk cache
export`, `pauk gui build`). Naming the sequence lets the worker ask for a
rebuild instead of building two command lines.
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

    Holds the graph throughout, because the snapshot reads Neo4j and a
    publish alongside would picture it half-written. Both builds are written
    at once - `public/` and `private/` under `config.gui_dir`.

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
