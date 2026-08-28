"""CLI entry point for loading a shared CSV export into Neo4j."""

import argparse
import logging
from pathlib import Path

from pymongo.database import Database

from pauk.jobs.locks import held
from pauk.jobs.models import GRAPH
from pauk.settings import Settings, settings
from pauk.storage import PreparedStore

from .audit import actor_context, audited_client
from .client import Neo4jClient
from .csv_loader import load_csv_dir
from .extract import NODE_REGISTRY
from .jsonl_loader import FILE_SPECS, load_prepared_rows
from .overrides import apply_overrides, tombstoned_ids, tombstoned_relationships
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


# Which node label the rows of each prepared entity become, derived from
# the loader's own map rather than written out again: an entity added to
# the pipeline (organizations, when department matching landed) must not
# silently lose its tombstones because a second list was never updated.
# persons.jsonl is the one file FILE_SPECS does not carry — it feeds two
# registry entries, ITMO and external, that share the base label.
FILE_LABELS = {
    filename: NODE_REGISTRY[spec_key].labels.split(":")[0]
    for filename, spec_key in FILE_SPECS.items()
} | {"persons.jsonl": "Person"}


def _drop_tombstoned(rows_by_file: dict[str, list[dict]], mongo_db: Database) -> dict[str, list[dict]]:
    """Remove rows whose node was deleted by hand.

    Filtering here rather than deleting after the load is what keeps the
    audit log honest: MERGE would recreate the node and apply_overrides
    would remove it again, writing a creation and a deletion nobody asked
    for on every single run.
    """
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


def load_jsonl_group(config: Settings, mongo_db: Database, group: str) -> None:
    """Load one prepared group from Mongo into Neo4j. Used by `pauk publish graph`.

    Takes the graph lock for the whole run. Two publishes at once interleave
    their batches and reapply manual decisions against a half-written graph,
    and the audit feed ends up describing changes in an order that never
    happened.

    Args:
        config: Application settings (Neo4j connection).
        mongo_db: The raw/prepared MongoDB database.
        group: The group whose prepared rows to publish.

    Raises:
        Busy: Something else is already writing the graph.
    """
    with held(mongo_db, GRAPH):
        _load_locked(config, mongo_db, group)


def _load_locked(config: Settings, mongo_db: Database, group: str) -> None:
    """The publish itself, with the graph already held.

    Split out so the lock wraps the whole run rather than each step: a
    second publish starting between the upload and apply_overrides would
    reapply decisions against a graph that is still being written.
    """
    prepared = PreparedStore(mongo_db, group)
    rows_by_file = {
        filename: list(prepared.read_rows(entity))
        for entity, filename in ENTITY_FILES.items()
    }
    rows_by_file = _drop_tombstoned(rows_by_file, mongo_db)
    client = audited_client(config, mongo_db)
    try:
        create_constraints(client)
        # Large batches are recorded as one summary entry each (see
        # AuditedNeo4jClient.diff_threshold), so a publish costs a handful
        # of audit rows, not one per node — but "who republished this group
        # and when" stops being invisible.
        with actor_context("etl-pipeline", source=f"publish:{group}"):
            # LinkCandidate is the one label with no prepared file of its
            # own — it is made up from repo_links rows — so _drop_tombstoned
            # cannot filter it and the loader is told separately.
            load_prepared_rows(client, rows_by_file, tombstoned_relationships(mongo_db),
                               tombstoned_ids(mongo_db, "LinkCandidate"))
            # Last step, after candidate promotion and every fold: publishing
            # overwrites hand-corrected fields with whatever the source says,
            # so the manual decisions are put back on top.
            apply_overrides(client, mongo_db)
    finally:
        client.close()


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
