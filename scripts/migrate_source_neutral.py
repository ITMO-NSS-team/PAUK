"""Plan or apply the source-neutral identity migration.

Dry-run is the default. Production apply belongs in a maintenance window
with the worker stopped, after rehearsing against Mongo and Neo4j copies.
"""

from __future__ import annotations

import argparse
import json

from pauk.migrations import SourceNeutralMigration
from pauk.settings import settings
from pauk.storage import get_mongo_client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--mongo-only", action="store_true",
        help="apply only Mongo backfill; skip Neo4j republish/dedup and derived artifacts",
    )
    args = parser.parse_args()
    client = get_mongo_client(settings)
    try:
        if args.apply:
            from pauk.jobs.store import running, workers_present

            workers = workers_present(client[settings.mongo_db])
            active_jobs = running(client[settings.mongo_db])
            if workers or active_jobs:
                raise SystemExit(
                    "source-neutral migration requires a maintenance window; "
                    f"active workers={workers}, active jobs={[job.id for job in active_jobs]}"
                )
        from pauk.graph.client import Neo4jClient

        graph = Neo4jClient(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
        )
        try:
            person_aliases = graph.fetch_merged_id_map("Person")
            graph_publications = graph.fetch_legacy_publications([
                row["id"] for row in client[settings.mongo_db].publications.find({}, {"id": 1})
            ])
            graph_persons = graph.fetch_legacy_persons([
                row["id"] for row in client[settings.mongo_db].persons.find({}, {"id": 1})
            ])
        finally:
            graph.close()
        migration = SourceNeutralMigration(
            client[settings.mongo_db],
            settings.pdf_dir,
            args.batch_size,
            person_aliases=person_aliases,
            graph_publications=graph_publications,
            graph_persons=graph_persons,
        )
        result = migration.apply() if args.apply else migration.plan()
        if args.apply and not args.mongo_only:
            from pauk.cache import GraphSnapshotExporter
            from pauk.graph.dedup import run_graph_dedup
            from pauk.graph.load import load_jsonl_group
            from pauk.gui.graph_builder.builder import write_site_data
            from pauk.storage import PreparedStore

            published = {}
            for group in PreparedStore.known_groups(client[settings.mongo_db]):
                published[group] = load_jsonl_group(settings, client[settings.mongo_db], group)
            result["neo4j_publish"] = published
            result["neo4j_dedup"] = run_graph_dedup(settings, client[settings.mongo_db])
            snapshot = GraphSnapshotExporter(settings).export()
            write_site_data(snapshot, settings.gui_dir, seed=42)
            result["snapshot"] = str(snapshot)
            result["gui_dir"] = str(settings.gui_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        client.close()


if __name__ == "__main__":
    main()
