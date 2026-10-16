# `pauk/storage/`: raw and prepared storage in MongoDB

**What it covers:** the pipeline's intermediate layer between `collect` and
`publish graph`: raw responses from external APIs and prepared entities,
stored in MongoDB. `data/static/` stays file-based: it is a versioned
reference catalog, not intermediate pipeline data.

**Files:** `pauk/storage/raw.py`, `prepared.py`, `mongo.py`, `review.py`,
`atomic.py`, `naming.py`, `static.py`, `llm_log.py`, `pdf.py`.

PDF bytes live on disk (`Settings.pdf_dir`); Mongo holds only a pointer (the
`pdfs` collection). `pauk/pipeline/stages/code_links.py` reads and writes
them through `pdf.py::PdfStore`.

A close-up of this layer (collections, both read paths, the three write
steps) is in [`../diagrams/mongodb-storage.md`](../diagrams/mongodb-storage.md).

## Entities are global, not per group

There is one Mongo document per entity. Its `_id` is the entity's stable id
(OpenAlex work id, a deterministic person id, and so on), and a `groups:
[...]` field lists the names of the groups that touched it. If two
overlapping selections hit the same publication, it is enriched once: the
enrichment stage of a new group sees the document already enriched by the
previous group (same `processing` status, `pdf_urls`, and so on).

**Reads are scoped to the group.** `read_rows` and `read_models` filter by
`{"groups": self.group}`: a stage sees and writes only documents its group
touched. A stage is therefore blind to rows that only another group wrote.
Do not assume that a row visible in Mongo is visible to a stage. A separate
pair, `get_rows` and `get_models(entity, ids, model)`, is a point lookup by id
**without** the group filter, for code that must see an entity's global state
even if its own group has not touched it yet (consumers:
`pauk/pipeline/normalize.py::OpenAlexNormalizer._seed` and
`pauk/graph/unmerge.py`).

What this does not solve: the same real-world entity under **different** ids
(several OpenAlex author ids for one person, an ORCID-fallback id, and so on)
is the job of the dedup stages (`enrich dedup`, `pauk dedup graph`).

## `raw.py::RawStore`

Append-only, collection `raw`, document `{source, group, fetched_at, request,
payload}`. `append(source, payload, request)` is an `insert_one` with no
upsert, so fetch history is never overwritten. `read(source)` is
`find({"source": source, "group": self.group})` sorted by `fetched_at`.

`ensure_indexes()` creates two compound indexes on `raw`: `{source, group,
fetched_at}` for the normal read, and `{source, fetched_at}` for cross-group
scans that also sort responses by time. Cross-group scans (for example
`pauk/graph/dedup.py::collect_raw_orcids`, "which ORCID does author A1 have
across all groups, latest fetch wins") are a plain `find({"source": ...})`
without a group filter.

## `prepared.py::PreparedStore`

Seven collections, `COLLECTIONS = {"publications": "publications", ...}`;
the collection name equals the entity name: `publications`, `persons`,
`departments`, `organizations`, `repositories`, `github_profiles`,
`repo_links`. `_id` is the value of the entity's key field (`KEY_FIELDS`):
`id` for all entities except `repo_links`, whose `RepoLink` has no `id` and is
keyed by `publication_id`.

- **`read_rows`/`read_models`**: `find({"groups": self.group})`, as plain
  dicts or validated pydantic models.
- **`get_rows`/`get_models`**: the same by specific ids (`$in`), with no group
  filter.
- **`write_rows`/`write_models`**: set the **complete** state of the group for
  an entity. Each given row is an `update_one(upsert=True)` with `$set` of all
  fields and `$addToSet: {groups: self.group}`. Then any row the group held
  before but did not reconfirm (folded by dedup, renamed on re-normalization)
  loses the group mark (`$pull`), and a document left with no group is
  deleted so it does not linger unreachable. Only the rows passed in are
  touched.
- **`upsert_models`**: persists changed rows without redefining the group's
  full membership; it never removes the group mark from untouched rows.
  Enrichment stages use it after an external request completes. Passing a
  subset to `write_models` would retract the group's claim on everything not
  in the subset, which is why `upsert_models` exists.

### Versioning: `_version` and `revisions`

Before writing, the store compares the new row content with what the document
already holds (ignoring `_id`/`groups`/`_version`). **If the content is
unchanged, nothing changes**: no `_version` bump, no history entry, only the
`$addToSet` of the group (provenance). This is deliberate: every stage rereads
and rewrites its whole working set on every run, and without the check the
version would grow on every no-op run instead of on real events.

If the content differs, the entire old document goes into the shared
`revisions` collection (one for all entities, like `raw`), and `_version`
grows by 1:

```python
{
    "entity_type": "publications",  # prepared collection name
    "entity_id": "W123",
    "version": 1,                   # the version number being archived
    "snapshot": {...},              # the full document as it was BEFORE replacement
    "replaced_by_group": "period-2024",
    "replaced_at": "2026-08-12T...",
}
```

The live document is the current version; `revisions` archives all earlier
ones. The index `{entity_type, entity_id, version}` is created by
`ensure_indexes`. Real versions appear where facts really change: dedup
merged two ids, the LLM reclassified a link (`--force`), a field was filled
in a later run.

The admin panel reads the archive on a record card, in the "Что говорил
источник" (what the source said) block (`pauk/admin/source.py`). It shows
transitions between versions, that is, what each run changed. The most recent
transition is computed against the live row; if the live row no longer
exists (dedup folded it into another), the most recent transition is not
shown, because comparing with an empty document would look like a run that
erased every field. The archive grows without bound and is trimmed by age
with `pauk admin trim` ([cli.md](cli.md)).

**Known simplification:** reading the previous state and writing it are two
separate Mongo requests (`find_one` + `update_one`, not an atomic
`find_one_and_update`). Truly concurrent writes to the same document from two
groups can race. In practice runs are started by hand and do not overlap. A
pipeline run holds its group (`PipelineRunner.run`) and publishing holds the
graph, but nothing guards a single document across groups.

## `mongo.py`

`get_mongo_client(config, timeout_ms=None) -> MongoClient` is a thin factory.
There is one client per process, opened at the command's entry point
(`pauk/cli.py`) and closed in `try/finally`. `timeout_ms` is how long to wait
for a server; the driver default is thirty seconds, which suits a command but
not a web request, so the admin panel passes a short one.

`ensure_indexes(db)` creates indexes on `revisions`, `raw`, `audit`,
`graph_overrides`, `jobs`, `job_workers` (TTL), `review_pairs` and others. It
is idempotent and called when each command starts, next to
`get_mongo_client`, the same place where Neo4j gets `create_constraints`.

`ensure_compression(db)` is called from `ensure_indexes` and creates `raw` and
`revisions` with zstd compression if they do not exist yet. They are the two
heaviest collections: verbatim API responses and full row snapshots. An
existing collection cannot change its compressor this way (WiredTiger takes
it at creation); the command for a live database is in
[admin-panel.md](../admin-panel.md). A server or test double that does not
accept the option keeps the default compressor.

## `review.py`: questions a person answers

Collection `review_pairs`: what the pipeline could not decide by itself, and
people's answers. It lives here rather than in `pauk/graph/` (unlike
`graph_overrides`) because the collection stage writes the questions long
before publishing, and `pauk/pipeline/` imports nothing from the graph layer.

One document per question, with a deterministic key: refusing the same pair
again updates the document instead of creating a second one.

| Field | Meaning |
|---|---|
| `_id` | `<kind>:<id>:<id>...` with members sorted, so (a, b) and (b, a) are one question |
| `kind` | `person_pair`, `person_group`, `github_person` or `staff_record` |
| `members` | who the question is about: person records, an account login, catalog records |
| `evidence` | what the rules gathered: names, shared coauthors, departments, refusal reasons (`held_because`); refreshed on every repeated refusal so there is something to compare against |
| `seen_at`, `source` | when the question was last asked and by whom: the collection stage (`stage`) or a graph pass (`graph`) |
| `verdict`, `actor`, `note`, `decided_at` | the answer: `same` or `different`, by whom, why, when |
| `applied_at` | when a "same" answer actually merged the records; empty while it waits for a run |
| `chosen` | for a catalog question, which record was picked |
| `skipped_at`, `skipped_by`, `disputed_at`, `disputed_rule` | the question was postponed; the rules changed their mind after a "different" answer |

An answer and its application are different things and live in different
fields. Applying changes the graph once; the answer keeps all later runs in
agreement with the person. A "different" answer has nothing to apply: it
exists so the rules do not merge the pair tomorrow when it gains a shared
coauthor.

`applied_at` is set by whoever merged: the panel (`mark_applied`) or a run
(`mark_applied_merges`, which matches answers against the map of what was
folded into what). A merged answer cannot be withdrawn (`withdraw` refuses);
it is taken apart with `record_undo` after `pauk.graph.unmerge.split_person`.

Indexes include `{verdict, seen_at}` (the queue opens on unanswered, oldest
first) and `{members}` (all questions about one person).

## PDFs: on disk, Mongo holds only a pointer (`pdf.py::PdfStore`)

`pauk/pipeline/stages/code_links.py` reads and writes PDFs through
`PdfStore(db, pdf_dir)`. The bytes are a plain file
`<pdf_dir>/<publication_id>.pdf` (`Settings.pdf_dir`, default `data/pdf/`),
flat with no per-group folder: like the publication itself, a PDF is tied to
the global id. Mongo stores only a pointer: collection `pdfs`, one document
per id, `{fetched_at}`. The path is not stored; it is derived from the id.

The cache is checked on disk, not in Mongo: `PdfStore.exists(id)` is
`path.exists()`, with no database query. If the file is missing it is
downloaded and written atomically (`atomic_write_bytes`), and only after the
write succeeds is the pointer upserted. The order matters: if the process dies
in between, a file may exist without a pointer, but a pointer never promises a
file that is not there.

`PdfStore.read()` is a single `path.read_bytes()` call, so no file handle
outlives it (important on Windows, where an open file cannot be deleted).
`_extract_pdf()` takes `bytes`, not a path.

## LLM logs: `llm_log.py::LlmLogStore`

The full request and response of every LLM call goes into a separate
collection **per use case**, not one shared collection with a tag:
`llm_logs_link_relevance` for link classification and `llm_logs_author_names`
for name splitting. New call sites should get their own collections the same
way.

```python
llm_log = LlmLogStore(self.prepared.db, "llm_logs_link_relevance")
llm_log.record(
    group=..., model=..., prompt=..., raw_response=client.last_response,
    parsed=result, usage=client.last_usage, error=..., context={...},
)
```

Every call is logged in full, with no diff filter (unlike prepared
versioning): each LLM call is a real event on its own. `author_names` writes a
separate document for the corrective semantic retry too; its number is in
`context.response_attempt`. `OpenRouterClient.last_response`
(`pauk/sources/llm.py`) is the raw OpenRouter body of the latest
`chat_json()`, alongside `last_usage`.

## `atomic.py`

- **`AtomicWriter`**: writes to a temporary file next to the target and
  replaces it via `os.replace()` only after a successful close. Used for
  snapshots (`pauk/cache/graph_snapshot.py`), the site data
  (`pauk/gui/graph_builder/builder.py`) and local journals such as the dedup
  candidates (`data/audit/<group>/dedup_candidates.jsonl`, path from
  `Settings.audit_dir`). Text mode only (`mode="w", encoding="utf-8"`); it
  does not suit binary data.
- **`atomic_write_bytes(target, data)`**: the same trick for bytes already in
  memory (PDFs from `PdfStore.save`).

## `naming.py`

- **`group_name(work_id=..., date_from=..., date_to=..., name=...)`**
  generates the group name: an explicit `name` wins, otherwise
  `<today>__<work_id>` or `<today>__from_<start>__to_<end>`.
- **`validate_group(group)`**: the regex `^[A-Za-z0-9][A-Za-z0-9_.-]*$`. A
  group is just a value of the `groups` field in Mongo, but the format stays
  restricted to something sensible.

## `static.py::StaticStore`

File-based: a versioned reference, not intermediate pipeline data. The
department catalog is read first from `data/static/departments.jsonl` if it
exists, otherwise from `data/static/departments_catalog.json`. A department's
id is the `uid` of its catalog entry; `parent` references another entry's
`uid` (an unknown parent raises `ValueError`), and entries with `kind:
organization` become `Organization` rows.

## Configuration

`MONGO_URI` (default `mongodb://localhost:27017`) and `MONGO_DB` (default
`pauk`) in `.env`, the same pattern as `NEO4J_URI`/`NEO4J_USER`/
`NEO4J_PASSWORD` in `pauk/settings.py`.
