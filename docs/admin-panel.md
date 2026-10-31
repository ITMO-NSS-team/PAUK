# Admin panel: running and internals

The panel edits the graph by hand so that an edit survives the next
`pauk publish graph`. It is a separate FastAPI service that runs next to the
databases.

The admin UI is in Russian. Button and page labels are quoted as they appear
in the UI, with an English gloss the first time.

## Quick start

### Start

Databases first: the panel does not start without Mongo, and shows no graph
without Neo4j. For an existing workstation setup with standalone database
containers:

```bash
open -a Docker                              # macOS, if the daemon is not running
docker start pauk-mongo pauk-neo4j-test
docker ps --format '{{.Names}}\t{{.Status}}'   # both must be Up
```

If the containers do not exist yet, see "Setup" and "No draft" below.

Then two processes, both from the repository root. The panel shows and edits;
the worker executes what the buttons start:

```bash
uv run pauk admin serve --host 127.0.0.1 --port 8600
uv run pauk admin worker
```

Open `http://127.0.0.1:8600`. Create an account first (the command prompts for
the password):

```bash
uv run pauk admin user add <login> --role admin
```

On the server these processes run as Compose services `admin` and `worker`,
with the panel on port 8505; see "On the server".

### Roles

| role | edits the graph, answers review questions | starts runs |
|---|---|---|
| `viewer` | no | no |
| `editor` | yes | no |
| `admin` | yes | yes |

`user add` without `--role` creates an `editor`; pass the role explicitly for
read-only accounts.

### Run the pipeline

On the "Пайплайн" (Pipeline) page, the **"Запустить конвейер"** (Run the
pipeline) button runs collect, publish and map rebuild as one job, in that
order. The page shows the current phase.

Two things the pipeline does not do, to be done by hand afterwards:

1. "Здоровье БД" (DB health) -> **"Пересчитать"** (Recalculate): the numbers
   there are a snapshot of the last calculation and do not refresh themselves.
2. "Пайплайн" -> **"Сверка с источником"** (Reconcile with source), first
   without the checkbox to see what publishing left in the graph after
   deletions in Mongo, then with it if the list looks right.

### The panel does not open

Locally, check the databases first (`docker ps`, `docker start ...`). Without
Mongo the panel does not start and says so in the window it was started from.

On the server, check that the processes are alive before restarting:

```bash
cd ~/pauk/code
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml ps
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml logs --since 30m admin worker
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8505/login
```

`200` confirms that the login page responds. If it fails, inspect Compose
status and logs before restarting. Check the job queue before stopping a worker.

### After a machine restart

On the server, Docker restarts the Compose containers unless they were
explicitly stopped. Locally, start the databases and the two development
processes again if they are not managed by Compose. Accounts, answers, audit
records and sessions live in Mongo and survive process restarts.

Deploy Python changes by rebuilding the PAUK image and recreating `admin`
and `worker`; a plain container restart still runs the previous image.

## Setup

Two databases:

- **MongoDB**: accounts, sessions, the audit log, manual decisions, review
  questions, the last health-check run and the job queue. The panel does not
  start without it.
- **Neo4j**: the graph itself. The panel runs without it; login and accounts do
  not depend on Neo4j.

Settings come from `.env` in the repository root, the same as for the
pipeline:

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=pauk
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=...
```

Optional: `PAUK_ADMIN_SECURE_COOKIE` sets the `Secure` flag on the session
cookie. Set it only when the panel is behind HTTPS; without TLS login stops
working.

There is no separate Neo4j user for the panel: permissions are enforced by the
application, not the database.

Local MongoDB, if you have none:

```bash
docker run -d --name pauk-mongo -p 27017:27017 -v pauk-mongo-data:/data/db mongo:7
```

Create an account (`uv run pauk admin user add <name> --role admin`). This step
matters beyond login: Mongo indexes are created by `pauk` commands, not by the
panel, so a panel started on a fresh database works but is slow.

### On the server

The checkout is `~/pauk/code`; services are defined in `deploy/compose.yml`
and configured through `~/pauk/secrets/pauk.env`. The panel listens on
`0.0.0.0:8505` inside the container, published on host port 8505.

```bash
cd ~/pauk/code
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml ps
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml logs --since 30m admin worker
curl -fsS http://127.0.0.1:8505/login -o /dev/null
```

Create accounts inside the running service:

```bash
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml exec admin pauk admin user add <login> --role admin
```

After an approved checkout update, apply it with `up -d --build` using the
same env file and Compose path. The worker handles SIGTERM by finishing its
current job; Docker allows five minutes before killing it. Prefer an idle
queue for updates. A terminated job is reaped after its lease expires; inspect
its state rather than assuming that a restart completed it.

Access is through the lab VPN over HTTP. Enable TLS and
`PAUK_ADMIN_SECURE_COOKIE=1` if the panel is exposed outside that boundary.
Sessions live in Mongo, so a panel restart does not log users out. The full
storage layout, service checks and site publication are in
[architecture/deploy.md](architecture/deploy.md).

## Worker

The panel never runs anything itself. A run takes minutes to hours, so a
button puts a job in the queue and a separate process executes it:

```bash
uv run pauk admin worker
```

Without the worker, jobs pile up and the "Пайплайн" page shows them all
waiting. This also means a panel restart cannot interrupt a publish. The worker
takes one job at a time. Flags: `--once` (take one job and exit), `--poll`
(pause on an empty queue), `--name` (the name recorded on jobs it takes;
default `host:pid`). On Ctrl+C it finishes the current run and exits.

Only the `admin` role can start runs.

### What can be run

The main button is **"Запустить конвейер"**: collect, publish and map rebuild
as one job. It cannot be three separate jobs, because publishing needs a group
name that does not exist yet when the job is queued.

The same steps are available individually below the button:

| step | what it does |
|---|---|
| "Собрать публикации" (Collect publications) | fetches works from OpenAlex and runs all enrichment stages; does not touch the graph |
| "Выложить группу в граф" (Publish the group to the graph) | loads the collected data into Neo4j and reapplies manual edits |
| "Пересобрать карту" (Rebuild the map) | graph snapshot and map data (`pauk cache export` + `pauk gui build`) into `data/gui/{public,private}` |

Two more jobs are separate cards and are not part of the pipeline: graph
deduplication (next section) and "Сверка с источником" (see "Reconcile with
Mongo").

A group is the result of one collect run; its name is generated from the date
and the selection. Publishing works on one group at a time; there is no
"publish everything" command in the panel or the CLI.

Each step shows when it last completed, which answers the usual question
"did anyone publish after the last collect?". The map rebuild writes both
variants: `private/` with personal fields and `public/` without
`authors-detail.json`. Publishing a built site is separate: nginx reads
`~/pauk/site/itmo/main`, not the worker's generated data directory. See
[deployment](architecture/deploy.md#static-site-and-reports).

### Two different dedups

`dedup` is both a collect stage and a separate button:

| | collect stage | button |
|---|---|---|
| sees | one group | the whole graph, all published groups |
| catches | duplicates within one collect | duplicates across **different** runs |

If 2024 was collected in January and 2025 in March, and one person appears in
both under different spellings, neither group's dedup sees the other. Only a
pass over the whole graph catches that pair.

Disputed pairs are not merged but held: written to
`data/cache/dedup_candidates_graph.jsonl` with status `held` and a reason, and
put in the "Спорные случаи" (Disputed cases) queue for a human answer.

The panel offers a split action for pairs merged through answers in the review
queue. Other merges do not get that action; deduplication asks for confirmation
before changing the graph.

## Review queue

"Спорные случаи" holds questions the pipeline cannot settle by evidence. Four
kinds:

| question | asked by | answer |
|---|---|---|
| are these two records one person? | person dedup: collect stage and whole-graph pass | "один человек" (one person) or "разные" (different) |
| a group rejected as a whole | same dedup, when a group has two ORCIDs, two emails or two staff-catalog records | mark who in the group is the same person |
| whose GitHub account is this? | `github_match` stage | "это его аккаунт" (this is their account) or "не его" (not theirs) |
| which catalog record? | dedup, when the staff catalog has several people with that name | choose a record ("это выбранная запись") or "никто из них" (none of them) |

Questions live in the Mongo collection `review_pairs`, one document per
question. If a later run hits the same question, it updates the evidence in the
existing document instead of creating a second one. The default tab is
"Приоритетные" (Priority): identical names without confirmation, groups,
accounts and the catalog. The rest is under "Все нерешенные" (All unresolved).

**An answer beats the rules, both ways.** Rules read answers before deciding
anything. "One person" merges the pair even with no evidence; "different"
blocks the merge even if a shared coauthor appears later. An answered question
is not asked again.

**When an answer takes effect.** It is recorded immediately. If both person
records are already graph nodes, the panel merges them right away. Otherwise
the next run that sees both does it: a collect of the same group, or graph
dedup after publishing. The queue shows "склеено" (merged) with a date, or
"склеится при следующем прогоне" (will merge on the next run). GitHub-account
and catalog answers are never applied by the panel itself: the next collect
binds the account, and the next collect or graph dedup uses the catalog
record. Neo4j is not needed to record an answer.

**When the rules change their mind.** If, after a "different" answer, the
evidence grows and a rule would now fire, the pair is not merged but goes to
the "Расхождения" (Discrepancies) tab with the rule that fired. Any answer
closes the discrepancy: confirm the old one or reverse it.

**Whole groups cannot be answered.** A group is rejected because it certainly
contains more than one person. You mark the ones who are the same person, and
the panel records pairwise answers: "one person" within the marked set,
"different" between the marked and the rest. Without the second half, the
rules would rebuild the same group through the remaining records.

**Skip and reverse.** "не знаю" (don't know) removes the question from the
unseen tab without answering it. An answer that has not merged anything can be
withdrawn with "передумать" (change my mind), and the question returns to the
queue.

**Splitting a merged pair.** Withdrawing an answer is not enough once the graph
has one node. **"разделить обратно"** (split back) rebuilds the removed record
from its independent prepared row or a durable pre-merge snapshot (node and
all its relationships), strips from the
surviving record what came only from the removed one, and records "different"
so the next run does not merge them again. `person_merge_sources` preserves
complete source rows before dedup removes them. The split restores prepared
rows and group membership too; missing source data or later changes that cannot
be reconciled make it refuse rather than overwrite enrichment. Merges done by dedup rules without a question
and merges via `pauk admin merge` are not in the queue and cannot be undone
from the panel.

## Queue and cancellation

Only one writer touches the graph at a time. Publish, dedup, map rebuild and
the pipeline take turns; a job that finds the graph busy goes back to the
queue. Collect locks only its own group and runs in parallel. The same lock
applies to terminal commands: `pauk publish graph` during a publish from the
panel reports who holds the graph and does nothing.

A waiting job can be cancelled and will not start. For a running job, the
cancel request is recorded and the worker notices it at the next seam: between
pipeline phases, between collect stages, between publish batches, and roughly
once a second inside most stages. A job is never dropped mid-write. Stages
that cannot stop from inside are listed in "Known limitations".

### If the worker disappears

A running job reports every minute that it is alive. If it stops, the process
is gone: worker stopped, machine down, badly timed Ctrl+C.

After five minutes of silence the page marks the job **"не отвечает"** (not
responding). That is a warning, not a verdict: the lock lives for fifteen
minutes, and the job may still be alive and writing while merely unable to
reach Mongo. The job is declared dead only after fifteen minutes, when the lock
lease has certainly expired. The worker does this: start it again and it
reaps such jobs on its first loop, marking a cancelled one as cancelled and
the rest as failed.

While a resource is busy, jobs that need it wait without blocking others;
collect by group proceeds even if a publish is waiting for the graph.

If the worker disappeared **in the middle of a publish**, manual edits in the
graph have already been overwritten by pipeline values and not restored, since
reapplication is the last step. The decisions themselves are intact; restore
them with:

```bash
uv run pauk admin overrides apply
```

The next successful publish does the same.

## Accounts

Accounts are created from the terminal only; the browser has no sign-up or
password change.

```bash
uv run pauk admin user add ivanov --role <admin/editor/viewer>   # create
uv run pauk admin user list
uv run pauk admin user disable ivanov
uv run pauk admin user enable ivanov
```

| role | can do |
|---|---|
| `viewer` | view only: search, node card, relationships, audit log, manual decisions, review questions, DB health, pipeline |
| `editor` | plus edit fields, create and delete records, link and unlink, answer review questions |
| `admin` | plus start runs: pipeline, collect, publish, dedup, reconcile, map rebuild, health checks |

Roles guard against mistakes, not against intruders: the role is a command
argument, so anyone with server access can grant themselves `admin`.

Passwords are stored as salted `scrypt` hashes. Login creates a session row in
MongoDB and sends the browser only a random token in an `HttpOnly` cookie. So
`disable` takes effect immediately: the sessions are deleted with the block
instead of living until the cookie expires.

Only the token's `sha256` is stored. The token is a pass: whoever holds it is
logged in, no password needed. In plaintext, a dump, backup or debug copy of
`admin_sessions` would be enough to act as someone else.

A session lasts 12 hours. After 30 consecutive failed attempts, login stops
answering for 15 minutes. The count is per login, not per address, because the
panel sits behind a VPN and often a single proxy.

Every edit is written to the audit log with the author, e.g. `user:ivanov`.

## No draft

"Сохранить" (Save) writes straight to Neo4j, the database named in `.env`.
There is no preview or deferred apply; the only ways back are
`pauk admin overrides undo` or a counter-edit.

So inspect live data as a `viewer`: it sees everything and can change
nothing. To practice editing, run your own Neo4j and point `NEO4J_URI` at it:

```bash
docker run -d --name pauk-neo4j-test -p 7688:7687 \
  -e NEO4J_AUTH=neo4j/testpass -v pauk-neo4j-test:/data neo4j:5
```

Port 7688 avoids a clash with a working database on 7687. Then in `.env`:

```
NEO4J_URI=bolt://localhost:7688
NEO4J_PASSWORD=testpass
```

The graph there is empty. Fill it by publishing (`pauk publish graph --group
<group>`) or create a node by hand with "Создать" (Create) on any label's
search page.

A hand-created node survives publishing on its own: the loader touches only
ids it has rows for. It is still recorded in `graph_overrides` as a "this
record was created by hand" claim. There is nothing to reapply, but without it
reconcile could not tell the record from stale data and would delete it. A
field edit on such a record is part of the same claim, so it cannot be undone
in "Ручные решения" (Manual decisions); delete the record instead.

## What the panel has

| section | what you can do |
|---|---|
| overview | graph labels, node and field counts per label |
| search | list of nodes per label, search by id and text |
| record card | all fields, edit, delete with confirmation, edit history in the graph and source-row history |
| new record | create a record the pipeline does not know |
| relationships | link to and unlink from another record, over the 11 allowed types |
| deleted record | what happened to it, and "Восстановить" (Restore) |
| "Журнал правок" (Audit log) | who changed what and when, via the panel and via the pipeline; filters and pages |
| "Ручные решения" (Manual decisions) | what is currently applied over the pipeline, each with "отменить" (undo) |
| "Расхождения" (Discrepancies) | where the source started saying something different from what it said before the edit |
| "Спорные случаи" (Review) | everything the pipeline is unsure about: person duplicates, GitHub accounts, namesakes in the catalog |
| "Здоровье БД" (DB health) | 32 cheap graph checks (gaps, suspicious names, duplicates, contradictions); per check, the records behind it and a CSV export |
| "Пайплайн" (Pipeline) | queue and run history, full pipeline or single step, a three-phase bar for full runs |

Things worth knowing:

- **Concurrent edits to one record are rejected.** If the record changed while
  the form was open, saving fails instead of silently losing the other edit;
  the page returns with the current values.
- **Undoing a deletion decision restores the record at once**, with all
  fields: a snapshot is stored in the decision when the record is deleted.
- **Creating a record with the id of a deleted one clears the deletion mark**,
  otherwise the next publish would remove it again.
- **The audit log and manual decisions are different things.** The log is
  history and nothing leaves it; decisions are what is in force now. An undone
  decision stays in the log but stops applying. One edit that survives three
  publishes yields seven log entries and one decision row.
- **A banner shows while a job is working on the graph.** Edits still go
  through, but a publish may overwrite them; the discrepancy then shows on the
  decisions screen.
- **Long values are collapsed.** A paper's full text shows its beginning, then
  a "развернуть" (expand) button.

Not in the panel, though available in the terminal:

- **Merging two arbitrary records** (`pauk admin merge`). The panel merges only
  pairs proposed by dedup, with their own queue, evidence and split-back.
- **Creating accounts** (`pauk admin user add`), deliberately.

### Health checks

Checks run as a job, not when the page opens: more than thirty queries over the
whole graph, some walking every person with regular expressions. The page shows
the result of the last run with its date, and "Пересчитать" queues a new run.
The records behind a given check are fetched from the graph immediately: the
query is bounded, and nobody needs last week's bad records.

### Reconcile with Mongo

Publishing synchronizes confirmed code links and reapplies manual deletions.
Other stale records and relationships need a separate reconciliation pass: with the "Сверка с источником" button on the "Пайплайн" page, or
from the terminal:

```bash
uv run pauk admin prune            # lists what is extra
uv run pauk admin prune --apply    # removes it
```

`--limit` sets how many items of each kind are printed (default 20). On the
page this is one job with a checkbox: unchecked, it only counts and puts the
numbers in the result; checked, it removes. Run it unchecked first and read the
list: manual records created before hand-made records were tracked look extra.

It leaves three things alone: records and links made by hand (they are in the
manual decisions, which is the only reason that record exists), what the next
publish will fold into another record, and records whose Mongo rows exist but
the loader skipped this time.

### Trimming histories

Two histories grow and are never trimmed on their own: the audit log (`audit`)
and the archive of replaced rows (`revisions`, a full document snapshot before
each real change). Trim them about twice a year:

```bash
uv run pauk admin trim            # counts what would go from both
uv run pauk admin trim --apply    # removes it
```

180 days are kept by default (`--keep-days`). Restoring a deleted record is
unaffected: its field snapshot is in the decision, and decisions are not
trimmed.

### Mongo compression

`ensure_indexes` creates new `raw` and `revisions` collections with zstd
compression. Existing collections keep their creation-time compressor;
changing it requires an explicit database maintenance operation. Do not run
`compact` as a routine health check. See
[storage.md](architecture/storage.md#mongopy).

### Known limitations

- An unlinked relationship is restored from the decision itself, not from
  history: the decision stores the relationship, the log only records that it
  appeared and disappeared.
- Source history appears on the record card and nowhere else; there is no
  "all changed rows" list, you start from a concrete record.
- A run stops at the next seam, not instantly. Inside a stage the request is
  checked about once a second; during publish, between batches. A stopped
  stage loses what it had not written; an interrupted publish is completed by
  the next one, because every write in it is a `MERGE`.
- Four stages cannot be stopped from inside and run to their end: `emails`,
  `repositories`, `dedup` and `github_match`. Cancellation is checked where a
  stage reports progress, and these four do not use the shared counter; they
  notice the cancel on exit.
- Publishing synchronizes confirmed code links and reapplies manual deletions.
  Other rows deleted from Mongo require a separate reconciliation pass.

## Two databases, one edit

An edit lives in two places: the value in Neo4j and the decision about it in
MongoDB. They are different DBMSs, so there is no shared transaction.

Handled from both sides:

- **before writing**, the panel checks that Mongo responds; if not, it refuses
  and the graph stays untouched;
- **if Mongo fails between the two writes**, the graph write is rolled back.

This covers every edit that leaves a decision behind: field change, node
deletion, linking and unlinking, creation and restoration.

The rollback for creation is pessimistic: it deletes the node even though
usually there was no deletion mark and the node would have been fine. Only the
database that is currently down could say, and a node that vanishes on the next
publish is worse.

A rollback is visible in the audit log next to the edit. One case remains where
even that fails (both databases down at once); then the response says
explicitly that the edit stayed in the graph without a decision and the next
publish will remove it.

## Linking nodes

On a node card, under the relationship table, the form offers only the
relationships allowed for that label, out of the eleven the graph knows. The
node whose card you opened is filled in; you enter only the other side.

What matters is **how the other side is addressed**, written in parentheses by
each option. Usually it is the id, with two exceptions:

| relationship | what to enter |
|---|---|
| `Publication -> MENTIONS_LINK -> Repository` | the repository `url`, not the id |
| `Repository -> OWNED_BY -> GitHubProfile` | the account `login`, not the id |

This is how the loader itself finds the target, and the panel repeats its rule.
If you use the wrong field, the panel returns to the form and says what to
enter; nothing breaks.

## Internals

The panel never edits the graph directly. All changes go through
`pauk/graph/mutations.py`, which has closed lists: 7 labels, 11 relationship
types and fields derived from `NODE_REGISTRY`. Neither a label nor a field name
can come from a form, since they are interpolated into Cypher; arbitrary
queries are impossible.

The "Пайплайн" page is the same: the job kind is checked against `JobKind`, the
group against those that have prepared rows, dates with the same selectors as
`pauk run`. No command line is ever assembled.

How edits survive publishing:

- **a field change** is written to `graph_overrides` and reapplied after every
  `publish` and every dedup;
- **node deletion** leaves a mark, otherwise `MERGE` would recreate the node on
  the next run;
- **relationship deletion** also leaves a mark, for the same reason;
- **creating a record or relationship** is written as a claim. There is nothing
  to reapply: the loader does not delete what it does not know about, so such a
  record or link survives publishing by itself. The claim is read only by
  reconcile, so that hand-made records are not mistaken for leftovers.

A field change also stores the previous value. The discrepancy screen relies
on it: when the source starts saying something different from what it said at
edit time, the panel shows both versions.

The audit log cannot fail the edit it describes. It is written after the change;
a failing sink must not return an error from the middle of an operation when
Neo4j is already updated. A lost entry goes to the application log in full and
the operation continues.

## Operations control plane

The optional OPS service uses the same admin accounts on port 8506 at `/ops/`.
It manages service observations, full database backups, site releases and
operation logs; it is separate from this graph editor. Only accounts with the
`admin` role can open OPS. Details: [architecture/ops.md](architecture/ops.md).
