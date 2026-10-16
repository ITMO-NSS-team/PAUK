# `pauk/cli.py`: commands

**What it covers:** reference for the `pauk ...` commands: what each does
and which flags it takes.

**Files:** `pauk/cli.py`, `pauk/admin/cli.py` (the `admin` subcommands).

A single argparse parser, no typer or click. The entry point is `pauk` (see
`[project.scripts]` in `pyproject.toml`) or `uv run python -m pauk.cli`.

```
pauk [--verbose] <command> ...
```

`--verbose` turns on DEBUG for `pauk` modules and a redacted trace of HTTP
requests from `urllib3.connectionpool`: values of sensitive query parameters
(`api_key`, `token`, `password`, URL signatures) are replaced with
`[REDACTED]`. The Neo4j logger stays at WARNING so Cypher parameters never
reach the output. Known secrets are also stripped from other diagnostic
messages.

## `run` / `collect`

```
pauk run     --work <id> | --works-file <file> | --from <date> --to <date>  [--name <name>]
pauk collect --work <id> | --works-file <file> | --from <date> --to <date>  [--name <name>]
```

`run` is `collect`, `normalize` and `enrich` (all stages) in one call. It
does **not** include `publish graph`: loading into the shared Neo4j stays a
separate manual step (`PipelineRunner.run()`). `--work`, `--works-file` and
`--from` + `--to` are alternative ways to select works; `--name` overrides
the generated group name.

Before collecting new works, `collect` repairs stored records whose author
list was cut off by the OpenAlex list endpoint limit
(`Collector.refetch_truncated()`, called inside every `collect`).

## `normalize`

```
pauk normalize --group <group>
```

Parses the raw MongoDB collection (`raw`, filtered by `group`) into the
prepared collections. Re-running on the same group is safe: it keeps
enrichment data and `_processing` already on existing rows and merges them
with the refreshed raw content (`OpenAlexNormalizer.run()`). Entities in
MongoDB are global: if the same work id already appeared in another group,
normalization sees its current state by id (`PreparedStore.get_models`), not
only what this group has accumulated. See [storage.md](storage.md).

## `enrich`

```
pauk enrich [stage] --group <group> [--input <id-file> --entity <entity>] [--force]
```

`stage` is the name of one stage (`persons`, `departments`, `code_links`,
`link_relevance`, `emails`, `repositories`, `repo_people`, `dedup`,
`github_match`, `author_names`; optionally `social_graph`) or `all` (the
default: the main stages in the order of `ALL_STAGES` in
`pipeline/stages/__init__.py`). `social_graph` is in `OPTIONAL_STAGES` and
runs only when named. `--group` is always required.

`--input` and `--entity` go together. `--input` narrows the run to specific
ids: a file with one id per line (the same format as `--works-file` for
`collect`). `--entity` names the prepared entity those ids belong to, one of
the keys of `PreparedStore.COLLECTIONS` (`publications`, `persons`,
`departments`, `organizations`, `repositories`, `github_profiles`,
`repo_links`).

`--force` reruns rows whose stage is already `COMPLETED`, `COMPLETED_EMPTY`
or `NOT_APPLICABLE`. Use it when the stage logic changed and rows already
marked complete must be recomputed.

### `--input` selection

`_selection_from_input(path, entity)` reads the file line by line (blank
lines skipped) and builds `PreparedSelection(entity, ids)`;
`EnrichmentStage.selected()` skips everything outside that set. The file can
live anywhere and come from anywhere (hand-written, exported by a query).

## `publish graph`

```
pauk publish graph --group <group>
```

Loads the group's prepared collections from MongoDB into Neo4j: constraints
first, then all nodes, then all relationships. See
[neo4j-graph.md](neo4j-graph.md). `python -m pauk.graph.load --dir <folder>`
is a separate tool for loading an external CSV export; it needs neither
MongoDB nor the pipeline.

## `dedup graph`

```
pauk dedup graph
```

Deduplicates persons, publications and repositories **across the whole
graph**, not one group. It is the only way to catch duplicates that reached
Neo4j from different runs and never sat side by side in one group. It is not
part of `run`; run it by hand once several groups have accumulated. See
[pipeline/dedup.md](pipeline/dedup.md).

## `admin`: manual graph edits

```
pauk admin [--actor <name>] <command> ...

pauk admin schema
pauk admin node show   <Label> <id>
pauk admin node create <Label> <id> [--set field=value ...] [--note <text>] [--once]
pauk admin node set    <Label> <id> --set field=value [--set ...] [--expect-updated-at <stamp>] [--note <text>] [--once]
pauk admin node delete <Label> <id> [--cascade] [--note <text>] [--once]
pauk admin rel add     <SrcLabel> <REL_TYPE> <TgtLabel> <src_id> <tgt_id> [--set ...] [--note <text>] [--once]
pauk admin rel delete  <SrcLabel> <REL_TYPE> <TgtLabel> <src_id> <tgt_id> [--note <text>] [--once]
pauk admin merge       <Label> <duplicate_id> <canonical_id> [--yes]
```

Edits go through `pauk/graph/mutations.py`: labels, relationship types and
fields are checked against a whitelist derived from `NODE_REGISTRY` (see
[neo4j-graph.md](neo4j-graph.md)). `pauk admin schema` prints that whitelist
and is the only command that needs neither Mongo nor Neo4j.

`--set` values are parsed as JSON when possible: `stars_num=10` gives a
number, `has_readme=true` a boolean, `first_name_en=Ivan` a string.

Every edit is audited with actor `user:<OS login>` (or the one given with
`--actor`) and source `admin-cli`.

`node set` and `node delete` do not just write to the graph: they record the
decision in `graph_overrides` and apply it, otherwise the edit would last
only until the next `publish graph`. `--once` skips recording the decision
(a one-off edit that the next publish will overwrite); `--note` stores the
reason.

`rel delete` is recorded as an instruction, because otherwise `MERGE` would
recreate the edge from the same prepared row on the next publish. `rel add`
and `node create` are recorded as a **claim**: there is nothing to reapply
(the record or link survives publishing anyway), but without a claim
`prune` cannot tell them from something the pipeline once created and
stopped asserting. With `--once`, both write to the graph without claiming.

```
pauk admin overrides list                                          # decisions in force
pauk admin overrides apply                                         # reapply them to the graph
pauk admin overrides undo <Label> <id>                             # stop applying; the decision record is kept
pauk admin overrides undo-rel <Src> <REL> <Tgt> <src_id> <tgt_id>  # restore a link
```

`overrides undo` also drops the claim: after it, `admin prune` treats a
hand-made record as a leftover. The panel does not drop claims; there they
disappear when the record or link itself is deleted.

`--expect-updated-at` guards against concurrent edits: pass the `updated_at`
that `node show` returned, and the edit is rejected if the node changed
since.

`merge` deletes the duplicate together with its relationships and cannot be
undone. The panel's "split back" button ("разделить обратно", in
`pauk/graph/unmerge.py`) rebuilds a `Person` from its prepared row, but only
for a pair merged through an answer in the "Спорные случаи" (disputed cases)
queue; a merge done with this command never enters that queue. The command
asks for confirmation; `--yes` skips it.

### `admin prune`: reconcile the graph with its source

```
pauk admin prune [--apply] [--limit N]
```

`publish graph` only adds. A row deleted from Mongo, or a link a row stopped
asserting, stays in the graph forever. This command finds such leftovers
and, with `--apply`, removes them. Without `--apply` it prints the list and
changes nothing; `--limit` (default 20) caps how many of each kind are
printed.

The expected state is not recomputed separately: the loader runs against a
stand-in client that writes nothing and records what it was asked to write,
so the answer matches what a publish would do by construction. Three things
are left alone: anything claimed by hand (see above), anything the next
publish will fold into another record, and records whose Mongo rows exist
but which the loader skipped this time.

The command holds the graph lock for the whole run, comparison and deletion
both: a plan against a moving graph is worthless. The panel offers the same
task as the "Сверка с источником" (reconcile with source) button.

### `admin trim`: shorten the two growing histories

```
pauk admin trim [--keep-days N] [--apply]
```

The change feed (`audit`) and the archive of replaced prepared rows
(`revisions`) only grow. The command removes entries older than `--keep-days`
(default 180) from both. Without `--apply` it only counts.

Restoring a deleted record is unaffected: a snapshot of its fields lives in
the decision itself, and decisions are not trimmed.

### `admin worker` and `admin user`

```
pauk admin worker [--once] [--poll <seconds>] [--name <name>]
pauk admin user add <login> [--role <role>]
pauk admin user list
pauk admin user enable <login>
pauk admin user disable <login>
```

`worker` performs the runs scheduled from the panel, one at a time; `--once`
takes at most one job and exits. `user` manages panel accounts (default
role `editor`). `user` needs MongoDB only. See [../admin-panel.md](../admin-panel.md).

## `cache export` and `cache inspect`

```
pauk cache export  [--output <path>] [--only <group>[,<group>...]]
pauk cache inspect [path_to_snapshot] [--table <table>] [--sample N]
```

`export` snapshots the current graph into
`data/cache/graph_snapshot_<dd-mm-yyyy>.json` (or the given path), the input
for `pauk gui build`. `--only` takes any of `persons`, `publications`,
`repos`, `departments`, `organizations` and re-reads only those entities'
tables from Neo4j, taking the rest from the newest snapshot.

`inspect` prints table sizes, per-field stats for `--table`, or `--sample N`
rows from it. The snapshot defaults to the newest in `cache_dir`. See
[cache.md](cache.md).

## `gui build`

```
pauk gui build [--cache <snapshot>] [--out-dir <folder>] [--seed 42]
```

Snapshot to layout to JSON for the `pauk/gui/web` site
(`pauk/gui/graph_builder/builder.py::write_site_data`). By default it takes
the newest snapshot from `cache_dir` and writes to `gui_dir`
(`data/gui/{public,private}`). See [gui.md](gui.md).
