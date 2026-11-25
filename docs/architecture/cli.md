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
pauk run     [--source openalex] --id <id> | --ids-file <file> | --from <date> --to <date>  [--name <name>]
pauk collect [--source openalex] --id <id> | --ids-file <file> | --from <date> --to <date>  [--name <name>]
```

`run` is `collect`, `normalize` and `enrich` (all stages) in one call. It
does **not** include `publish graph`: loading into the shared Neo4j stays a
separate manual step (`PipelineRunner.run()`). `--work`, `--works-file` and
`--from` + `--to` are alternative ways to select works; `--name` overrides
the generated group name. `--work` and `--works-file` remain aliases for
`--id` and `--ids-file`. Collection is implemented only for OpenAlex in this
release; the selector names are source-neutral for future adapters.

Before collecting new works, `collect` repairs stored records whose author
list was cut off by the OpenAlex list endpoint limit
(`Collector.refetch_truncated()`, called inside every `collect`).

## `normalize`

```
pauk normalize --group <group> [--source openalex]
```

Parses the raw MongoDB collection (`raw`, filtered by `group`) into the
prepared collections through the adapter/identity/aggregate orchestrator.
Re-running an unchanged record is a no-op; a changed record resets dependent
enrichment centrally. Entities in
MongoDB are global: if the same work id already appeared in another group,
normalization sees its current state by id (`PreparedStore.get_models`), not
only what this group has accumulated. See [storage.md](storage.md).

## `enrich`

```
pauk enrich [stage] --group <group> [--input <id-file> --entity <entity>] [--force]
```

`stage` is the name of one stage (`persons`, `departments`, `arxiv_versions`,
`code_links`, `link_relevance`, `emails`, `repositories`, `repo_people`, `dedup`,
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

## `versions link` / `versions report`

```
pauk versions link [--llm] [--force]
pauk versions report [--output <path>]
```

Both find publication pairs that look like versions of one work under
different DOIs, which `dedup` keeps as separate nodes (preprint → publication,
original → translation, conference paper → journal article, a revision under
its own DOI), plus look-alikes explained by their bucket (an erratum, a cover,
a series entry). Neither merges anything.

`link` takes the graph lock and writes every pair as an
`(:Publication)-[:IS_VERSION {bucket, ...}]->(:Publication)` edge, removes
edges the rules no longer produce, and puts mergeable pairs into the review
queue (`/review`, "Версии публикаций"). `--llm` asks the LLM
(the standard `PAUK_LLM_MODEL`) about MEDIUM_FUZZY pairs no earlier run asked
about; `--force` asks again. The admin panel runs it as the `versions` job.

`report` only reads the graph (MongoDB is not needed) and writes the journal
(by default `data/cache/version_candidates_report.jsonl`, MEDIUM_FUZZY
unrouted); `link` writes its own routed one to `data/cache/version_candidates.jsonl`.
See [pipeline/version-candidates.md](pipeline/version-candidates.md).

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
`pauk/graph/unmerge.py`) rebuilds a `Person` from its prepared row or durable
pre-merge snapshot, but only
for a pair merged through an answer in the "Спорные случаи" (disputed cases)
queue; a merge done with this command never enters that queue. The command
asks for confirmation; `--yes` skips it.

### `admin prune`: reconcile the graph with its source

```
pauk admin prune [--apply] [--limit N]
```

`publish graph` adds and updates, synchronizes confirmed `MENTIONS_LINK` and
`IMPLEMENTS` relationships, folds duplicates and reapplies manual deletions.
Other stale records and relationships need reconciliation. This command finds
those leftovers and, with `--apply`, removes them. Without `--apply` it prints the list and
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

### `admin serve`, `admin worker` and `admin user`

```
pauk admin serve [--host <address>] [--port <port>]
pauk admin worker [--once] [--poll <seconds>] [--name <name>]
pauk admin user add <login> [--role <role>]
pauk admin user list
pauk admin user enable <login>
pauk admin user disable <login>
```

`serve` runs the panel's web server (port 8505 by default) through uvicorn,
with uvicorn's own lines in PAUK's log layout. `worker` performs the runs
scheduled from the panel, one at a time, and tags its lines with the job in
hand; `--once`
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

## `similarity collect`

```
pauk similarity collect --snapshot <graph_snapshot.json> --out <corpus-dir> [--refresh]
```

Builds a standalone corpus for repository similarity. The input supplies
canonical repository IDs, URLs and metadata. Collection uses the README
response cache populated by repository enrichment and backfills missing
entries through GitHub. The snapshot's historical `has_readme` value is
informational. A 404 is cached until refresh or a later enrichment.
Transient failures and unusable payloads are retried on the next run.
Backfill may request a Git blob by SHA for files whose inline bytes are
omitted and whose declared size is within the 2 MiB limit.

The output contains `blobs/<sha256>` with the original bytes, immutable
`versions/<sha256>.json` manifests and the latest complete
`manifest.json`. Each record contains its acquisition status, source path
and SHA, prepared text, text digest, source fragments (README line numbers
are one-based) and any skip reason. Counts include failed fetches even
when a description yielded usable text. Use a private output directory:
original READMEs can include personal information. Markdown, RST, AsciiDoc
and plain UTF-8 prose are handled by a line-based cleaner. Other encodings
and binary input are flagged and the description remains available.

## `similarity index`

```
uv sync --extra similarity
pauk similarity index --corpus <corpus-dir-or-version.json> --out <index-dir> [--batch-size 8] [--max-tokens 512] [--text-variant cleaned|fork]
```

The command encodes prepared corpus texts with the pinned BGE-M3 revision
`5617a9f61b028005a4858fdac845db406aefb181`, with 512 input tokens by
default. The first use downloads the model. `--max-tokens` accepts 3–8192
and changes the cache identity; longer inputs increase memory requirements.
The model and optional dependencies must run on the chosen machine.

The manifest records the corpus and snapshot fingerprints, model revision,
runtime versions, row IDs and text hashes. The matrix is a normalized
float32 NumPy array under `matrices/`, with immutable version manifests in
`versions/`. Per-text checkpoints in `cache/` resume interrupted builds.
The latest manifest is replaced atomically only after the matrix and version
manifest are written. Empty or skipped corpus records have no vector.
This command does not calculate repository pairs or change the map.
`--text-variant fork` uses name, description, topics and the first 4000
characters of the original README, matching the comparison input from
the search prototype. It uses the same eligible IDs as the cleaned corpus
and validates the original README blob digest. This is an experimental
comparison, not a quality judgment.

## `similarity neighbors`

```
pauk similarity neighbors --index <index-dir-or-version.json> --out <neighbors-dir> --top-k 3 [--min-score 0.5] [--mutual]
```

Checks the index manifest, matrix digest, dimensions, numeric values and
normalization before comparing every pair with a float64 dot product.
The output `manifest.json` and immutable `versions/<sha256>.json` contain
the index, corpus and snapshot digests. For each indexed repository,
`candidates` retains its directed top-K with scores and ranks before the
threshold. A pair passes the threshold inclusively and is emitted once when
it occurs in either directed list, or both with `--mutual`. Pair records
include the rank in each direction (null if outside that list) and both text
digests, which identify the source records in the corpus manifest. Ties
are resolved by repository ID. A top-K union can give a repository more
than K undirected neighbors. An empty index yields an empty result. The
default threshold is -1. Choose a display threshold after evaluation.
This command does not modify the graph.

## `similarity pool`

```
pauk similarity pool --corpus <corpus-dir> --index <index-dir> --graph <graph-data.json> --out <new-dir> [--sample-size 60] [--top-k 3] [--seed 42] [--fork-index <index-dir>]
```

Creates a blinded review CSV and separate ranking metadata. The corpus and
index must match exactly. The graph's repository ID set is checked against
the corpus and its SHA-256 is recorded; the caller must supply a GUI build
made from the same snapshot. A seeded sample attempts to select equal numbers
of isolated and linked repositories, then combines the top-K dense and
TF-IDF results for each. TF-IDF uses the same prepared texts, sublinear
term frequency, unigrams and bigrams, and a 35% maximum document frequency.
It does not include zero-score candidates. Only candidate generation is
performed; the output has no automatic quality judgments.
`--fork-index` adds results from a fork-style index built from the same
corpus; its input hashes are checked against the original README blobs.

`review.csv` contains pairs, prepared texts, original README excerpts and
their paths and blob SHAs, without method, scores or
existing-edge labels. Mark `judgment` as `same_function`, `related`,
`unrelated` or `insufficient`, describe the shared function, and mark
duplicates or forks separately. `pool.json` records methods, ranks, scores,
existing edges, selected repository IDs, parameters and input digests. Keep
it away from reviewers during blind labeling. Both files can contain README
text and must be handled as private working data. Existing `review.csv` is
never replaced: use a new output directory for another run. CSV text cells
are capped at 32,000 characters for spreadsheet compatibility and flagged
when truncated; the complete prepared text and original README blobs remain
in the corpus. Prepared text is displayed in full up to the CSV limit, while
the dense model sees only the configured maximum number of tokens.

## `similarity score`

```
pauk similarity score --pool <pool.json> --review <review.csv> --out <new-report.json>
```

Reports precision and Wilson 95% intervals for each method over judged
new pairs whose duplicate/fork status is explicitly `no`. Empty judgments
and undecided duplicate status are counted separately. `insufficient` is
judged but does not count as a match. Reports also retain hashes of the
pool and review file. This is precision on the labeled pool, not recall
over all possible repository pairs. The Wilson interval treats judgments
as independent, although pairs can share repositories; use it as a rough
descriptive interval rather than a definitive confidence guarantee.

## `reports build`

```
pauk reports build [<repo id> ...] [--cache <snapshot>] [--force] [--workers 6]
```

Runs OSA on every repository of the snapshot that has no report yet, except
third-party ones (cited, never `IMPLEMENTS`, no ITMO contributor), and
writes `data/gui/private/reports/<lang>/<repo id>.pdf` plus the site's
`reports/index.json`. Repository ids limit the run, `--force` regenerates
existing reports. Needs `OPENROUTER_API_KEY` and OpenRouter access; on the
server use `OPENROUTER_PROXY_URL`. The worker image includes `uvx`.
Exits non-zero if any report failed. `PAUK_REPORTS_DIR` sets another folder.
See [reports.md](reports.md).

## `gui build`

```
pauk gui build [--cache <snapshot>] [--out-dir <folder>] [--seed 42] [--similarity <neighbors.json>] [--similarity-layout]
```

Snapshot to layout to JSON for the `pauk/gui/web` site
(`pauk/gui/graph_builder/builder.py::write_site_data`). By default it takes
the newest snapshot from `cache_dir` and writes to `gui_dir`
(`data/gui/{public,private}`): `private/` for the lab server, `public/`
(masked names, no personal or service fields) for GitHub Pages. See
[gui.md](gui.md).
