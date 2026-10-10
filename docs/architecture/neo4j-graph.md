# `pauk/graph/`: connector and the actual graph schema

**What it covers:** the Neo4j graph schema as the code builds it, and how
the connector works: prepared rows from MongoDB to nodes and relationships,
loading, and graph-level dedup.

**Files:** `pauk/graph/extract.py`, `jsonl_loader.py`, `client.py`,
`audit.py`, `mutations.py`, `overrides.py`, `schema.py`, `csv_loader.py`,
`dedup.py`, `load.py`, `pauk/urls.py`.

The same schema as a Mermaid diagram:
[`diagrams/neo4j-schema.md`](../diagrams/neo4j-schema.md). Field-by-field
description of all nodes and relationships:
[`diagrams/neo4j-schema-desc.md`](../diagrams/neo4j-schema-desc.md).

## Nodes and relationships

`Publication.pdf_urls` holds the ordered unique PDF links as a Neo4j string
array. In the `versions` ledger each version has its own `pdf_urls`.

| Node | Unique key | Labels |
|---|---|---|
| Person | `id` (bare OpenAlex author ID) | `Person` |
| Department | `id` (the catalog `uid`) | `Department` |
| Organization | `id` and `name_en` (both unique) | `Organization` |
| Publication | `id` (bare OpenAlex work ID) | `Publication` |
| Repository | `id` and `url` (both unique) | `Repository` |
| GitHubProfile | `id` and `login` (both unique) | `GitHubProfile` |
| LinkCandidate | `id` (the URL itself) | `LinkCandidate` |

```text
(:Person {is_itmo: true})  -[:BELONGS_TO]->     (:Department)
(:Person {is_itmo: true})  -[:AUTHORED]->       (:Publication)
(:Person {is_itmo: false}) -[:AUTHORED]->       (:Publication)
(:Person {is_itmo: true})  -[:CONTRIBUTED_TO]-> (:Repository)

(:Department)  -[:PART_OF]->       (:Department | :Organization)

(:Publication) -[:PRODUCED_BY]->   (:Department)
(:Publication) -[:MENTIONS_LINK]-> (:Repository | :LinkCandidate)

(:Repository) -[:DEVELOPED_BY]-> (:Department)
(:Repository) -[:IMPLEMENTS]->   (:Publication)
(:Repository) -[:OWNED_BY]->     (:GitHubProfile)
```

Besides `name`, `url`, `description` and `stars_num`, `Repository` carries
`topics`, `language`, `forks_num`, `archived`, `is_fork`, `license` and
`last_updated`. They come in the same `GET /repos/{owner}/{name}` response as
the main fields, at no extra request
([pipeline/repositories.md](pipeline/repositories.md)).

`AUTHORED` carries `position`, `affiliation`, `affiliation_source` and
`is_corresponding`; `CONTRIBUTED_TO` carries `role`; `MENTIONS_LINK` carries
`context` (a list), `page_number` (a list; `0` means the abstract, because
Neo4j cannot store `null` inside an array property, so the sentinel is not
`None`; see [pipeline/code-links.md](pipeline/code-links.md)),
`is_relevant`, `classification_status`, `llm_confidence` and `llm_reason`.

For links extracted with line-break alternatives, `MENTIONS_LINK` also
stores `url_ambiguous`, `candidate_urls` (a flat list of strings) and
`availability`. An ambiguous occurrence does not confirm an authored
repository: the edge's `is_relevant` stays `null` and it does not create
`IMPLEMENTS`. The original break fragment stays in the prepared data.

`MENTIONS_LINK` records the bare fact that a link is present and is created
for `is_relevant` of `true`, `false` and `null`. `IMPLEMENTS` has stronger
semantics: the repository is a confirmed authored result of the publication,
so it is built only from `Repository.publication_ids`, which holds the links
with `is_relevant=true`.

`CONTRIBUTED_TO` is built by the `github_match` stage: a confirmed account is
the same person, so the account's repositories become their work. `role` is
`owner` if the account owns the repository, otherwise `contributor`. The edge
is created only to a repository present in the prepared `repositories`
collection: the social graph walks uncited repositories too but does not
store them, and a relationship cannot point at a missing node.

The evidence the matcher decides on (`GitHubProfile.emails`, `commit_names`,
`repos`, and `Person.emails`) is not published to the graph. These are
working data for matching and also real people's addresses; they stay in the
prepared rows and in the `github_matches.jsonl` journal. Of the collected
profile, only `company` goes into the graph.

A person always has the single label `:Person`; ITMO membership is the
boolean property `is_itmo`, not a label. The property is sticky: the same
author can be ITMO in one group and external in another, but `is_itmo` only
grows, and an external row never lowers an existing `is_itmo=true`
(`n.is_itmo = coalesce(n.is_itmo, false) OR row.is_itmo` in
`client.py::upsert_person_nodes_batch`). In cross-group dedup the same rule
is applied by `BOOLEAN_MERGE_FIELDS["Person"]` in `_fold_nodes_batch`.

The department hierarchy is recursive: each `Department` is `PART_OF` exactly
one parent, either another `Department` (`parent_id`) or a root
`Organization` (`organization_id`). A chain such as department, faculty,
megafaculty, organization is therefore a chain of edges of one type.

## `extract.py`: declarative registry

`NODE_REGISTRY: dict[str, NodeSpec]` has one recipe per prepared row type. A
`NodeSpec` carries a whitelist of simple properties (`prop_fields`; anything
not listed does not reach the node, which keeps stray fields such as debug
values out) and a list of `RelSpec`: which row fields are really hidden
relationships. A `RelSpec` can describe:

- a **scalar field** (`scalar=True`): one value, not a list
  (`Repository.owner_login` to `OWNED_BY`);
- a **list of bare ids** (`tgt_id_field=None`), for example `department_ids`;
- a **list of objects with relationship properties**: `authored` to
  `AUTHORED`, with `prop_fields=("position", "affiliation", ...)`;
- a **discriminated field** (`guard`): `mentions_links` / `repo_links` lead
  either to `Repository` or to `LinkCandidate`, told apart by `target_kind`;
  these are two `RelSpec`s on one data field, each with its own `guard`.

`extract_node` and `extract_relationships` are pure `dict -> dict` functions
that never touch the network, so they are tested without a live Neo4j
(`tests/unit/test_graph_extract.py`).

Neo4j cannot store nested maps or lists of maps (`funding`, `versions`,
`affiliations`, `counts_by_year`) as node properties, so `extract_node`
serializes them to JSON text (`JSON_TEXT_FIELDS`).

## `jsonl_loader.py`: load order

Hard rule: **all** nodes are loaded first, then **all** relationships. If a
relationship refers to a node that does not exist, it is simply not created
(a warning with the exact count goes to the log). A placeholder node is never
created; that is deliberate.

`repo_links` is not a node and is handled separately
(`extract_repo_links()`): each link's URL is compared with known
`Repository.url` values (through `normalize_repo_url`: case-insensitive,
ignoring `www.`, trailing slash and `.git`). A match gives `MENTIONS_LINK` to
the `Repository`; otherwise a `LinkCandidate` is created on the fly and the
link points to it. This is the **actual** path links take into the graph;
`Publication.mentions_links` is not read at all, even though a recipe for it
exists in `NODE_REGISTRY`.

A `repositories` row whose `repositories` stage ended as `failed` is skipped:
the `name`/`url` stub from an unsuccessful GitHub API request is not loaded
until a retry succeeds, and the link to it stays a `LinkCandidate`.

At the end of every load, `promote_link_candidates_batch` handles the case
where an earlier publish created a `LinkCandidate` while GitHub was
unavailable and the repository has since resolved: the old relationships move
to the `Repository` with their properties, and a candidate left with no other
links is deleted. `fetch_merged_id_map` runs for each label: if this publish
brought an id that graph dedup had already folded into another node, it is
folded again immediately, without waiting for the next `pauk dedup graph`.

After loading and alias folding, `sync_implements_relationships_batch`
reconciles existing `IMPLEMENTS` edges with the publication's full set of
confirmed repositories. Stale edges are removed only for publications whose
`link_relevance` ended as `completed` or `completed_empty`. A `failed` status
removes nothing: a transient model error must not destroy the last confirmed
state of the graph.

## `client.py`: talking to Neo4j

Batched `UNWIND ... MERGE`, in chunks of `CHUNK_SIZE = 2000`.
`upsert_relationships_batch` returns the number of source/target pairs that
actually matched. The Neo4j counter `relationships_created` is no good for
this: it stays `0` when `MERGE` finds an existing relationship (a rerun),
which is normal, not an error.

`Neo4jClient.__init__` does not create constraints. That is a separate
explicit step, `schema.create_constraints()`, before the first data run. An
empty password raises a clear `ValueError` instead of a late authentication
error from the driver.

`_fold_nodes_batch` is the shared duplicate-folding mechanism (used by
`merge_person_nodes_batch`, `merge_publication_nodes_batch` and
`merge_repository_nodes_batch`, called from `pauk/graph/dedup.py`). It moves
all outgoing and incoming relationships from the duplicate to the canonical
node (an existing relationship of the canonical node wins; the duplicate's
properties only fill gaps: `SET new += properties(old); SET new += keep`).
Node fields go through Python logic in `_merge_duplicate_properties` (lists
are unioned preserving order, booleans OR-ed, JSON lists unpacked, unioned and
repacked), then the duplicate is `DETACH DELETE`d. Node properties cannot be
merged directly in Cypher with the same trick: it would briefly set
`canonical.id` to the duplicate's id while the duplicate still exists, and
fail on the uniqueness constraint.

## `audit.py`: change log

`AuditedNeo4jClient` is a transparent wrapper around `Neo4jClient`. It
intercepts only mutating methods (`upsert_*_batch`, `merge_*_batch`,
`delete_*_batch`, `sync_implements_relationships_batch`,
`promote_link_candidates_batch`); everything else (`fetch_*`, `close`, access
to `driver`) goes to the wrapped client through `__getattr__`. For each
intercepted call it takes a snapshot of the affected nodes and relationships
**before**, makes the call, takes a snapshot **after**, diffs the fields and
writes to an `AuditSink`. If the wrapped call raises, nothing is logged:
the audit never claims a change that did not happen.

The actor (who) and source (from where) come from `contextvars`, not from an
argument: `actor_context("user:...", source="admin-ui")`. The loader and any
CRUD code do not have to pass the actor through every signature; one `with`
around the calling code is enough.

Batches of `diff_threshold` (default 50) rows or more write one coarse
`bulk_write` entry (a count only) with no per-field diff: diffing every node
of a two-thousand-row ETL chunk would double the number of queries for almost
no audit value. Smaller batches get a full `AuditEntry` per row with `diff:
dict[field, (before, after)]`. `created_at` and `updated_at`
(`TECHNICAL_DIFF_FIELDS`) are excluded from the diff in all three branches
(`created`, `updated`, `deleted`).

There are two sinks, used together (`MultiAuditSink`, assembled by
`build_audit_sink`):

- `JSONLAuditSink`: append-only JSONL, `audit.jsonl` in `audit_dir`
  (`{timestamp, actor, source, operation, entity_type, entity_id,
  change_kind, diff}` per line). It stays because it is easy to grep from a
  shell. Without a Mongo database, only this sink is used.
- `MongoAuditSink`: the `audit` collection, indexed by `(entity_type,
  entity_id, timestamp)` and `(actor, timestamp)`. A file cannot do what the
  panel's change feed needs: filtering and pagination. Values BSON cannot
  accept (the driver returns its own types such as `neo4j.time.DateTime`) are
  stored as text, since the audit must never be what crashes a write.

`audited_client(config, db)` is how the graph should be opened wherever it is
modified. `publish graph` and `dedup graph` open it this way.

One gap remains: an audit entry is written *after* the Neo4j transaction
commits, as a separate step, so a crash in that narrow window leaves a graph
change with no entry. Closing it fully would require writing the audit in the
same transaction as the data (a possible `Neo4jAuditSink` writing `:AuditEvent`
nodes in the same `execute_write`; it does not exist).

## `mutations.py`: manual graph edits

Everything that changes the graph outside the pipeline goes through this
module: the `pauk admin` commands and the panel's routes. Callers keep only
argument parsing and `actor_context`; what counts as a valid edit is defined
here and nowhere else.

Why a separate layer instead of calling `Neo4jClient` from a handler:

- **Labels and relationship types reach Cypher by interpolation.** They are
  identifiers and cannot be passed as parameters; `client.py` builds
  `f"MERGE (n:{label} ...)"` as a string. While the source of those strings
  is our own literals, that is fine; once the source is an HTTP request,
  the lack of a whitelist becomes an injection. The whitelists (`NODE_FIELDS`,
  `RELATIONSHIPS`) are derived from `extract.py::NODE_REGISTRY` rather than
  rewritten beside it: a field the loader does not publish cannot be set by
  hand either.
- **The same rules for all callers.** A CLI and a web form, each with its own
  validation, would drift apart.
- **Concurrent editors.** `update_node` accepts the `updated_at` the editor
  saw when opening the record and rejects the write if the node has changed
  since. Otherwise two people silently overwrite each other, and the audit
  shows two legitimate edits.

Service fields (`id`, `created_at`, `updated_at`) cannot be edited: `client.py`
sets them itself, and they are excluded from the editable list, otherwise
`pauk admin schema` would promise a field every write rejects.

`merge_nodes` appends `merged_ids` to the canonical node **before** folding:
without that, `fetch_merged_id_map` cannot hold the duplicate down, and the
next publish would create it again. The operation is irreversible: the
duplicate is deleted with its relationships, and the audit diff covers node
properties only, so there is nothing to restore the edges from. The CLI
therefore asks for confirmation.

## `overrides.py`: edits that survive publishing

Neo4j is a display copy, not the source of truth: `publish graph` pours the
prepared documents into it with `MERGE ... ON MATCH SET n += row.properties`.
A field corrected by hand and also written by the pipeline is overwritten on
the next publish, and a node deleted by hand comes back because `MERGE`
creates it again.

Publishing has no reverse side: it only adds and updates, and deleting is not
its job. Mongo cleans itself (a row no group claims is deleted), so the
display copy diverges from the source in one direction. `pauk admin prune`
([cli.md](cli.md)) reconciles them, as a separate on-demand step.

So a manual edit is stored as a **decision**, not only as a value in the
graph: one document per target in the `graph_overrides` collection, and the
decisions are reapplied after every publish and every graph dedup.

| Field | Meaning |
|---|---|
| `_id` | `node:<Label>:<id>`, deterministic, so editing the same node updates one document |
| `op` | `set`, `delete`, or `create` (a claim on a hand-made record) |
| `fields` | what to set; **merged** with what is already stored, not replacing it |
| `created` | the record was created by hand; unlike `op`, a delete does not overwrite it, and restoring uses it to bring the claim back |
| `auto_value` | what the field held before the first edit, for the conflicts screen |
| `snapshot` | for a deletion, every field the node carried, so restoring does not depend on the audit feed |
| `active` | cleared on undo; the document stays so the panel can show that an edit existed |

Applying (`apply_overrides`) must be idempotent: the value is compared with
what is already in the graph and no write is made if they match. Otherwise
every publish would rewrite the overrides and stamp audit entries for
changes that never happened.

`op: "delete"` also works as a tombstone: `load.py::_drop_tombstoned` drops
such rows **before** loading. If filtering happened afterwards, `MERGE` would
create the node and reapplying would delete it, so the state would be right
while every run showed a create and a delete nobody made.

Call sites: right when an edit is saved (`pauk admin node set`), at the end
of `publish graph`, at the end of `dedup graph`, and manually with
`pauk admin overrides apply`.

**Claims.** `op: "create"` on a node and `op: "link"` on an edge are not
instructions but claims: the record or link was added by a person. There is
nothing to reapply (the loader never deletes what it does not know about),
but without a claim `pauk admin prune` cannot tell a hand-made record from
something the pipeline once created and stopped asserting. `apply_overrides`
skips claims on links; for a claim on a node it reapplies the fields, which
are values a person chose. Editing a field of a hand-made record keeps it a
claim (`op` is not overwritten with `set`); otherwise undoing the edit would
drop the claim and the next reconciliation would delete the record.

**Relationships.** Deleting an edge is recorded as an instruction (`kind:
"rel"`, `op: "delete"`): a deleted edge would be recreated by `MERGE` from the
same prepared row, so `load_prepared_rows` accepts a set
`dropped_relationships` and discards those pairs before loading, for the same
reason as node tombstones. The decision key is the five-tuple `(source label,
type, target label, source id, target id)`, where the target id is in the form
the loader looks it up by: `url` for a repository, `login` for a GitHub
profile.

Order in the CLI matters: the edit goes to the graph first and only then is
recorded as a decision. Otherwise a rejected edit (version conflict or
validation) would leave a decision that the next publish silently applies,
doing exactly what the person was just refused.

## `csv_loader.py`

A parallel path for a shared CSV format (`id/labels/properties`), kept in
reserve. No pipeline stage produces such a CSV today, so there is nothing to
load with it, but the code works.

## `dedup.py`: graph-wide dedup

See [pipeline/dedup.md](pipeline/dedup.md), which also covers in-group dedup.
The merge rules are shared; only the source of rows (Cypher instead of
MongoDB) and the place where the result is written differ.

## `load.py`

The entry point of `pauk publish graph`: `load_jsonl_group(config, mongo_db,
group)` reads the group's prepared collections from MongoDB
(`PreparedStore.read_rows`) and passes the rows to
`jsonl_loader.load_prepared_rows` (a shared function that does not care where
rows come from). Separately, `uv run python -m pauk.graph.load --dir
<folder>` runs a standalone tool for an external CSV export, with no MongoDB,
using `csv_loader.load_csv_dir`. Both paths create constraints, then load,
and close the connection in `finally`.

## `urls.py`

The one function outside `graph/`, `storage/` and `pipeline/` used by both
the pipeline and the graph layer: `normalize_repo_url()`, the comparison key
for repository URLs (case, `www.`, trailing slash and `.git` are cosmetic;
without normalization the same repository would split into a `Repository` and
a `LinkCandidate`).
