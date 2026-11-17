# `pauk/cache/`: graph snapshot

**What it covers:** how the Neo4j graph is dumped to a snapshot file on disk,
which `pauk/gui/` then reads.

**Files:** `pauk/cache/export.py`, `graph_snapshot.py`, `inspect.py`,
`snapshot_schema.py`, `__init__.py`.

This is the only step in the `pauk/gui` chain that talks to Neo4j.
Everything else (`pauk/gui/graph_builder/builder.py`) reads the snapshot
from disk, not the database.

## `export.py`

`GraphSnapshotExporter.export(path=None, only=None)` opens a driver, runs
the queries behind `load_db()` (one per table, fourteen in all), and writes
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
which `--only repos` does not pick up. If retained tables in the base snapshot
are missing or structurally incompatible,
the export fails before connecting to Neo4j and asks for a full run. Tables being
re-read may be missing or incompatible; the merged result is validated before writing.

`_execute_retrying()` is the shared retry loop for `ServiceUnavailable`,
`SessionExpired`, `TransientError` and `OSError`: up to `CYPHER_RETRIES = 5`
attempts with a growing pause.

`cypher_dict()` returns rows as dicts keyed by Cypher column names for every
table. `load_db()` validates the returned rows against `SNAPSHOT_COLUMNS` in
`snapshot_schema.py`, including partial exports. A query change that drops a
required column fails during export. A unit test also compares the actual query
aliases with the contract, including queries returning no rows.

`load_db(driver, tables=None)` returns a flat dict of fourteen tables:
`persons`, `publications`, `repositories`, `departments`, `organizations`,
`authorship`, `person_depts`, `pub_depts`, `repo_pubs`, `mentions_repos`,
`mentions_candidates`, `repo_persons`, `repo_depts`, `pub_versions`. The
first ten are what `pauk/gui/graph_builder/builder.py::GraphDataBuilder`
consumes; `organizations`, `mentions_repos`, `mentions_candidates` and
`pub_versions` (IS_VERSION: `version_pid` is a version of `pid`, with its
`bucket`, `mergeable` and review `status`) have no consumer yet. Author departments and repository owners are relationships
(`BELONGS_TO`, `OWNED_BY`) in the graph model, not columns, so each gets its
own query with `OPTIONAL MATCH`.

## `graph_snapshot.py`

`write_snapshot` (atomic) and `read_snapshot` write and read the flat
`load_db()` dict as JSON; `read_snapshot` raises `ValueError` if the top level
of the file is not an object. Both functions validate all fourteen tables using
`SNAPSHOT_COLUMNS`: tables must be lists, every row must be an object containing
all exported columns. Empty tables, null property values, extra columns and extra
tables are allowed. Missing columns are not filled with defaults: a missing
`forks_num`, for example, indicates an incompatible export, not a zero fork count.
Errors identify the table, the row (one-based), missing columns and the rebuild
command `pauk cache export`. The flat JSON format has no version wrapper or
stored manifest; compatibility is checked against the actual rows. Empty tables
contain no rows to validate. Updating the export columns requires updating the
shared contract and its tests.

`GraphDataBuilder.build()` repeats validation before assignment or layout, so
callers passing a dict directly receive the same early error. Validation checks
structure and column presence, not property value types or graph referential
integrity. An invalid write leaves the existing snapshot untouched. Snapshots are named
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
