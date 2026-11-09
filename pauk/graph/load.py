"""Publish prepared MongoDB groups or external CSV files to Neo4j."""

import argparse
import logging
from pathlib import Path

from pymongo.database import Database

from pauk.jobs.locks import held
from pauk.jobs.models import GITHUB_MATCH, GRAPH
from pauk.settings import Settings, settings
from pauk.storage import PreparedStore

from .audit import actor_context, audited_client
from .client import Neo4jClient
from .csv_loader import load_csv_dir
from .extract import NODE_REGISTRY
from .jsonl_loader import FILE_SPECS, Progress, load_prepared_rows
from .overrides import apply_overrides, claimed_relationships, tombstoned_ids, tombstoned_relationships
from .schema import create_constraints

logger = logging.getLogger(__name__)

# PreparedStore.COLLECTIONS keys -> the filenames load_prepared_rows expects.
ENTITY_FILES = {
    "organizations": "organizations.jsonl",
    "departments": "departments.jsonl",
    "publications": "publications.jsonl",
    "repositories": "repositories.jsonl",
    "github_profiles": "github_profiles.jsonl",
    "persons": "persons.jsonl",
    "repo_links": "repo_links.jsonl",
}


# Derive tombstone labels from the loader registry to avoid a second mapping.
# Persons use two specs with the same label and are absent from FILE_SPECS.
FILE_LABELS = {
    filename: NODE_REGISTRY[spec_key].labels.split(":")[0]
    for filename, spec_key in FILE_SPECS.items()
} | {"persons.jsonl": "Person"}


def _drop_tombstoned(rows_by_file: dict[str, list[dict]], mongo_db: Database) -> dict[str, list[dict]]:
    """Skip manually deleted nodes before MERGE can recreate them and add audit noise."""
    filtered = {}
    for filename, rows in rows_by_file.items():
        label = FILE_LABELS.get(filename)
        tombstones = tombstoned_ids(mongo_db, label) if label else set()
        if not tombstones:
            filtered[filename] = rows
            continue
        kept = [row for row in rows if row.get("id") not in tombstones]
        logger.info("publish: %d row(s) in %s skipped as deleted by hand",
                    len(rows) - len(kept), filename)
        filtered[filename] = kept
    return filtered


def load_jsonl_group(config: Settings, mongo_db: Database, group: str,
                     report: Progress | None = None) -> dict[str, int]:
    """Publish one MongoDB group with graph writes and GitHub ownership changes locked.

    Returns:
        Prepared row counts per entity and manual override results.

    Raises:
        Busy: Another run holds the graph or GitHub matching lock.
    """
    with held(mongo_db, GRAPH), held(mongo_db, GITHUB_MATCH):
        return _load_locked(config, mongo_db, group, report)


def _load_locked(config: Settings, mongo_db: Database, group: str,
                 report: Progress | None = None) -> dict[str, int]:
    """Keep both locks through loading and reapplying manual overrides."""
    prepared = PreparedStore(mongo_db, group)
    rows_by_file = {
        filename: list(prepared.read_rows(entity))
        for entity, filename in ENTITY_FILES.items()
    }
    client = audited_client(config, mongo_db)
    try:
        create_constraints(client)
        rows_by_file = _github_context(prepared, client, rows_by_file)
        rows_by_file = _drop_tombstoned(rows_by_file, mongo_db)
        with actor_context("etl-pipeline", source=f"publish:{group}"):
            # Generated LinkCandidates have no prepared rows for the tombstone filter.
            load_prepared_rows(client, rows_by_file, tombstoned_relationships(mongo_db),
                               tombstoned_ids(mongo_db, "LinkCandidate"), report=report,
                               claimed_relationships=claimed_relationships(mongo_db))
            # Manual decisions take precedence over published source values.
            counts = apply_overrides(client, mongo_db)
    finally:
        client.close()
    return {f"rows_{filename.removesuffix('.jsonl')}": len(rows)
            for filename, rows in rows_by_file.items()} | counts


def _github_context(prepared: PreparedStore, client, rows_by_file: dict[str, list[dict]]) -> dict[str, list[dict]]:
    people = rows_by_file["persons.jsonl"]
    related = {member for row in people if row.get("github_match")
               for member in [row["id"], *(row.get("merged_ids") or [])]}
    if not related:
        return rows_by_file
    aliases = client.fetch_merged_id_map("Person")
    known = {row["id"] for row in people}
    checked = set()
    while related - checked:
        for duplicate, canonical in aliases.items():
            if duplicate in related or canonical in related:
                related.update((duplicate, canonical))
        pending = related - checked
        checked.update(pending)
        # A graph fold may have independent source rows outside this group.
        for row in prepared.get_rows("persons", pending, include_merged=True):
            related.update((row["id"], *(row.get("merged_ids") or [])))
            if row["id"] not in known:
                people.append(row)
                known.add(row["id"])
    repository_ids = {item["repository_id"] for row in people for item in [
        *(row.get("contributed_to") or []),
        *((row.get("github_match") or {}).get("cleared_contributions") or []),
    ]}
    repositories = rows_by_file["repositories.jsonl"]
    known_repositories = {row["id"] for row in repositories}
    for row in prepared.get_rows("repositories", repository_ids, include_merged=True):
        if row["id"] not in known_repositories:
            repositories.append(row)
            known_repositories.add(row["id"])
    return rows_by_file


def main() -> None:
    """CLI entry point: `uv run python -m pauk.graph.load`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Load a shared CSV export into Neo4j.")
    parser.add_argument("--dir", default=None, help="Input directory (default: data root)")
    parser.add_argument("--uri", default=settings.neo4j_uri)
    parser.add_argument("--user", default=settings.neo4j_user)
    parser.add_argument("--password", default=settings.neo4j_password)
    args = parser.parse_args()

    client = Neo4jClient(args.uri, args.user, args.password)
    try:
        create_constraints(client)
        load_csv_dir(client, Path(args.dir or settings.data_dir))
    finally:
        client.close()


if __name__ == "__main__":
    main()
