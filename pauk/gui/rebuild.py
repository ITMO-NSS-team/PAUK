"""Rebuilding the map: snapshot, data files, statistics.

Three steps that were only ever run one after another by hand. Naming the
sequence lets the worker ask for a rebuild instead of building three
command lines.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pymongo.database import Database

from pauk.cache import GraphSnapshotExporter
from pauk.gui.generate_data import write_graph_files
from pauk.gui.generate_stats import write_stats
from pauk.jobs.locks import held
from pauk.jobs.models import GRAPH
from pauk.settings import Settings

logger = logging.getLogger(__name__)


def rebuild_map(config: Settings, mongo_db: Database, *, public: bool = False,
                seed: int = 42, snapshot_path: Path | None = None) -> dict[str, int]:
    """Export a fresh snapshot and write every file the map is served from.

    Holds the graph throughout, because the snapshot and the checks both
    read Neo4j and a publish alongside would picture it half-written.

    Args:
        seed: Layout seed. Held steady between runs, or a rebuild moves a
            map people navigate by shape.
        snapshot_path: Reuse an existing snapshot instead of exporting one.

    Returns:
        Counts of the map and of the graph, named apart: the map leaves out
        publications with no ITMO author.
    """
    with held(mongo_db, GRAPH):
        return _rebuild_locked(config, public=public, seed=seed, snapshot_path=snapshot_path)


def _rebuild_locked(config: Settings, *, public: bool, seed: int,
                    snapshot_path: Path | None) -> dict[str, int]:
    out_dir = config.map_out_dir(public)
    if snapshot_path is None:
        snapshot_path = GraphSnapshotExporter(config).export()
        logger.info("map rebuild: snapshot at %s", snapshot_path)
    counts = write_graph_files(snapshot_path, out_dir, seed=seed, public=public)
    counts.update(write_stats(out_dir))
    logger.info("map rebuild: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts
