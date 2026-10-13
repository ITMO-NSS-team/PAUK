# Architecture overview

**What it covers:** the end-to-end data flow through the whole project and a
table of `pauk/` subpackages with links to their own notes. Read this first,
before any other note in `architecture/`.

**Files:** nothing specific: an overview across all of `pauk/`; see the links
below for the details of each subpackage.

PAUK collects ITMO publications from OpenAlex, enriches them with data from
external sources (Crossref, ORCID, GitHub), finds links to code in them, and
loads the result into Neo4j. `pauk/gui/` reads the graph and draws an
interactive map.

It is a single Python package, `pauk/`.

## Data flow

The same flow as a detailed Mermaid diagram:
[`../diagrams/pipeline-flow.md`](../diagrams/pipeline-flow.md).

```
OpenAlex API
  |  pauk collect
MongoDB: raw                                 RawStore, append-only
  |  pauk normalize
MongoDB: publications/persons/...            PreparedStore, 7 collections
  |  pauk enrich [stage]                     persons -> departments -> code_links -> link_relevance ->
  |                                          emails -> repositories -> repo_people -> dedup ->
  |                                          github_match -> author_names
MongoDB: publications/persons/...            the same collections, enriched
  |  pauk publish graph
Neo4j                                        accumulates across runs, MERGE
  |  pauk dedup graph (on demand)
Neo4j                                        folds duplicates across groups
  |  pauk cache export
data/cache/graph_snapshot_<dd-mm-yyyy>.json  snapshot on disk
  |  pauk gui build
data/gui/{public,private}/*.json             data for the pauk/gui/web site
```

`<group>` is the name of one run: `<date>__<work_id>` for a single
publication, `<date>__from_<start>__to_<end>` for a period, or an explicit
name via `--name` (`pauk/storage/naming.py::group_name`). A group is a value
of the `groups` field on MongoDB documents; entities are global, not per group
(see [storage.md](storage.md)). The prepared collections are `publications`,
`persons`, `departments`, `organizations`, `repositories`, `github_profiles`
and `repo_links` (`PreparedStore.COLLECTIONS`).

**Neo4j is the only place where data from different runs accumulates.** Each
`pauk publish graph --group <group>` pours its group in through `MERGE ...
ON CREATE / ON MATCH`. Reading only the latest group would lose everything
collected earlier, which is why the cache and the visualization read the
graph, not files on disk.

## Subpackages

| Package | What it does | Details |
|---|---|---|
| `pauk/models/` | pydantic schema of the prepared rows | [models.md](models.md) |
| `pauk/sources/` | HTTP clients for external APIs | [sources.md](sources.md) |
| `pauk/storage/` | raw and prepared data in MongoDB | [storage.md](storage.md) |
| `pauk/pipeline/` | collect, normalize, enrich, dedup | [pipeline/overview.md](pipeline/overview.md) |
| `pauk/graph/` | prepared rows to Neo4j, graph-level dedup | [neo4j-graph.md](neo4j-graph.md) |
| `pauk/gui/` | snapshot to layout to static site | [gui.md](gui.md) |
| `pauk/cache/` | graph snapshot to disk, which gui then reads | [cache.md](cache.md) |
| `pauk/cli.py` | `pauk ...` commands | [cli.md](cli.md) |
| `pauk/admin/` | admin panel and worker | [../admin-panel.md](../admin-panel.md) |
| `scripts/` | one-off and operations utilities | [scripts.md](scripts.md) |
| - | deploying to the lab server | [deploy.md](deploy.md) |

`pauk/settings.py` is a single `Settings` dataclass for everything: data
paths, API keys, Neo4j parameters. It reads `.env` (see `.env.example` in the
repository root).

## Resumability

Every prepared-layer pydantic model carries `processing: dict[str,
ProcessingState]` (alias `_processing` in JSON): the status of each
enrichment stage on that row: `not_started`, `completed`, `completed_empty`,
`not_applicable`, `failed`. `EnrichmentStage.needs_attempt()`
(`pauk/pipeline/stages/base.py`) decides whether to touch a row again:
`FAILED` and `NOT_STARTED` yes, anything else no, unless `--force` is passed.
This makes `pauk enrich` resumable between runs; `write_models()` persists the
result at the end of each `run()`. Because entities in MongoDB are global,
`processing` is visible to the next group that touches the same entity, so
reprocessing does not start from zero (see [storage.md](storage.md)).

## Publishing to the graph

`pauk publish graph --group <group>` reads that group's prepared collections
from MongoDB and loads all nodes first, then all relationships. If a
relationship refers to a node that was not loaded, it is not created (a
warning with the exact count goes to the log; no placeholders are created).
`--input` for a targeted run over a subset of ids belongs to `enrich`, not
`publish`; see [cli.md](cli.md). Details: [neo4j-graph.md](neo4j-graph.md).

## Deduplication: two levels

- **Within one group** (`pipeline/stages/dedup.py`, the `dedup` stage, always
  part of `pauk enrich` and `pauk run`): local and offline; it folds persons,
  publications and repositories that matched within this run's rows.
- **Across the whole graph** (`pauk/graph/dedup.py`, `pauk dedup graph`, a
  separate command, not part of a normal run): the same merge rules, but it
  reads all of Neo4j, catching duplicates that entered the graph from
  **different** groups and never met in one group.

Both use the same decision functions (`plan_person_merges` and the paired
logic for publications and repositories); they differ only in where rows come
from (MongoDB vs Cypher) and where the result is written. See
[pipeline/dedup.md](pipeline/dedup.md).
