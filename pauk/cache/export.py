from __future__ import annotations

from pathlib import Path

from neo4j import GraphDatabase

from pauk.settings import Settings

from .graph_snapshot import write_snapshot


class GraphSnapshotExporter:
    def __init__(self, config: Settings) -> None:
        self.config = config

    def export(self, path: Path | None = None) -> Path:
        # Kept separate from GUI formatting: this is a normalized graph snapshot.
        from pauk.gui.generate_data import load_db

        target = path or self.config.cache_dir / "graph_snapshot.json"
        driver = GraphDatabase.driver(
            self.config.neo4j_uri,
            auth=(self.config.neo4j_user, self.config.neo4j_password),
        )
        try:
            driver.verify_connectivity()
            write_snapshot(target, load_db(driver))
        finally:
            driver.close()
        return target
