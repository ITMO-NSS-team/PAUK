# `pauk/cache/`: graph snapshot

**What it covers:** how the Neo4j graph is dumped to a snapshot file on disk,
which `pauk/gui/` then reads.

**Files:** `pauk/cache/export.py`, `graph_snapshot.py`, `inspect.py`,
`__init__.py`.

This is the only step in the `pauk/gui` chain that talks to Neo4j.
Everything else (`pauk/gui/graph_builder/builder.py`) reads the snapshot
from disk, not the database.

## `export.py`

`GraphSnapshotExporter.export(path=None, only=None)` opens a driver, runs
the queries behind `load_db()` (one per table, thirteen in all), and writes
`data/cache/graph_snapshot_<dd-mm-yyyy>.json` (or the path given with
`--output`). An empty Neo4j password raises `ValueError` immediately, before
the driver gets a chance to fail late.

**Partial export (`--only`).** A full export takes about an hour, almost all
of it spent on `persons`, `publications`, `authorship` and `pub_depts`. If
only one entity changed in the graph (say, repositories and their links were
deleted), `pauk cache export --only repos` re-reads just that entity's tables
(`SNAPSHOT_GROUPS` in `export.py`), takes the rest from the newest snapshot,
and writes a new file with today's date. The old snapshot is not modified.

A group is an entity plus every relationship table it takes part in: a
deleted person still present in `authorship` would be a dangling edge.
`--only` accepts a comma-separated list of `persons`, `publications`,
`repos`, `departments`, `organizations`. The log shows before and after row
counts for each re-read table.

Use it only when the change is confined to the named groups. Deleting
repositories also changes publication properties (`has_code`, `code_url`),
which `--only repos` does not pick up. If the base snapshot lacks a table
that is not being re-read, the export fails and asks for a full run.

`_execute_retrying()` is the shared retry loop for `ServiceUnavailable`,
`SessionExpired`, `TransientError` and `OSError`: up to `CYPHER_RETRIES = 5`
attempts with a growing pause. Two thin wrappers sit on top of it:

- `cypher()` returns rows as positional tuples, for tables whose shape is
  stable (`publications`, `repositories`, ...);
- `cypher_dict()` returns rows as dicts keyed by Cypher column names. It is
  used only for `persons`, the one table that keeps gaining fields, so a new
  column does not require changing positional unpacking in callers.

`load_db(driver, tables=None)` returns a flat dict of thirteen tables:
`persons`, `publications`, `repositories`, `departments`, `organizations`,
`authorship`, `person_depts`, `pub_depts`, `repo_pubs`, `mentions_repos`,
`mentions_candidates`, `repo_persons`, `repo_depts`. The first ten are what
`pauk/gui/graph_builder/builder.py::GraphDataBuilder` consumes;
`organizations`, `mentions_repos` and `mentions_candidates` have no consumer
yet. Author departments and repository owners are relationships
(`BELONGS_TO`, `OWNED_BY`) in the graph model, not columns, so each gets its
own query with `OPTIONAL MATCH`.

## `graph_snapshot.py`

`write_snapshot` (atomic) and `read_snapshot` write and read the flat
`load_db()` dict as JSON; `read_snapshot` raises `ValueError` if the top level
of the file is not an object. Snapshots are named
`graph_snapshot_<dd-mm-yyyy>.json` (`dated_snapshot_path`), and
`latest_snapshot` picks the newest by the date in the name. That is the
default for `pauk cache inspect` and `pauk gui build`; `gui build` logs the
path it chose. There is no TTL check: the age is visible in the file name.

## `inspect.py`

Backs `pauk cache inspect [path] [--table T] [--sample N]`. Without
arguments it prints each table's row count; with `--table` it prints
per-field null counts and types (JSON-text fields are marked and sorted by
sparsity); with `--table` and `--sample N` it prints the first N rows with
JSON-text fields decoded.
