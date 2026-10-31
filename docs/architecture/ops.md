# OPS: operations control plane

OPS manages Docker services, full database backups and static-site releases.
It runs separately from the graph editor: `uvicorn pauk.ops.app:build --factory`.
Its UI is `/ops/`, with JSON endpoints under `/api/ops/`. The source is
`pauk/ops/` and `pauk/gui/web/src/features/operations/`.

## Process and permissions

Compose profile `ops` adds the `ops` service on port 8506 and is enabled on
the lab deployment. Both
nginx and OPS use `restart: unless-stopped`. Docker restarts exited processes;
an unhealthy probe alone does not trigger a restart. An intentional stop stays
stopped. The OPS health endpoint checks HTTP availability; the status page
performs the separate service and data checks.

The OPS image builds its frontend with no `publicDir`: it contains neither
private graph JSON nor laboratory source catalogs. FastAPI serves that build.
The graph map continues to run in the existing nginx container on 8520–8529.
The services do not share a release lifecycle.

OPS has no authentication: the UI and all API endpoints are available without
accounts, cookies or session tokens. Anyone with network access to port 8506
can run operations. Access is provided through the laboratory network/VPN.
Login, logout and graph-editor routes are absent. Manually started operations
are recorded with actor `anonymous`; destructive actions still require typed
confirmation. The frontend does not send credentials.

OPS starts independently of the admin service and MongoDB readiness. Its
journal and settings remain available when MongoDB is down; database probes
report failures. Operations that use MongoDB still require it to be reachable.

OPS has the Docker socket and access to PAUK storage, so the container is a
privileged control plane. Expose it only inside the laboratory VPN. The browser
cannot submit commands, paths or executable names. Server commands are fixed;
backup/release IDs and archive ports are validated. The read-only checkout and
secrets mounts use their absolute host paths so Compose bind mounts refer to
the same locations when executed through the host Docker daemon.

## Journal and execution

`PAUK_OPS_STATE`, normally `~/pauk/ops`, stores `ops.sqlite3`, per-operation
`.log` files and a process lock. It is outside the managed databases and is
not rolled back with a database backup. SQLite serializes admission: only one
queued or running operation is accepted. A single runner executes the queue.
The process lock prevents a second runner from opening the same state directory.
Run one Uvicorn worker.

Stages and operation results persist across HTTP requests and reloads. On an
OPS restart, an interrupted operation is marked failed and is not replayed.
Inspect service state before retrying an interrupted maintenance operation;
there is no automatic destructive replay. A normal shutdown waits for the
current operation; Compose allows ten minutes before forcefully stopping it.
Do not update OPS during database maintenance.

The API returns the most recent 200 operations and the tail of each command
log, redacting configured credentials. Log files are private to the control
plane. The operation dialog and downloaded log contain server output, not
simulated progress. Running operations are not cancellable from the UI.

## Observed state

The status API reads Compose state, container CPU/memory, filesystem usage,
Mongo ping, a real Neo4j query, the worker heartbeat, published map JSON and
the GitHub token. Failed probes remain failed; a missing observation is not
converted to a successful status. essdata usage describes the entire shared
filesystem, not the user's quota. No OSA controls or report generation are
part of OPS.

The frontend requests state every 15 seconds while visible. Form contents
are preserved during polling. Below 1024 CSS pixels, or on devices whose
primary pointer is coarse and cannot hover, OPS shows a desktop-only screen
and does not poll or expose controls. The graph map's own UI is separate.

## Backups and restore

Managed backups live in `PAUK_ESSDATA/dumps/<stand>/<id>/` and contain:

- `mongo.archive.gz`: all MongoDB databases, users and roles;
- `neo4j.dump` and `system.dump`;
- `manifest.json`: creation time, label, checksums and verification results.

The configured root Mongo URI is passed through the process environment;
credentials do not appear in host command arguments. Never publish backup
files or serve their directory through nginx.

Before maintenance, OPS refuses an active pipeline job and takes the graph
lock. It stops the worker and editor, rechecks active jobs, then stops Neo4j.
Mongo remains running for `mongodump`; this supports the standalone Mongo 4.4
installation without replica-set-only `--oplog`. Pipeline writers are stopped;
do not run database-mutating CLI commands during maintenance. Services that
were already stopped remain stopped. A `finally` block restarts the services
OPS stopped, including when a dump fails.

Verification reads all SHA-256 hashes, the full gzip stream and metadata of
both Neo4j dumps using `neo4j-admin database load --info`. This does not claim
that a restore has been tested. A restore first verifies the selected backup
and matches its database-credential fingerprint to the current environment.
It then creates a new full safety backup. Failure to create that copy prevents
any restore writes.

Restoration drops the PAUK Mongo database before loading the archive, so
collections created after the backup are removed. It loads both Neo4j dumps
offline, restores data ownership, restarts services and checks Mongo and a
Neo4j query. Only then is `restored` recorded. There is no automatic rollback
of a partially failed database restore; the safety copy and operation log are
the recovery inputs. Restoring `system` also restores Neo4j users and roles.

The schedule is disabled by default. Enabling it explicitly approves the
maintenance interruption shown in the dialog. It runs once per Moscow calendar
day after the chosen time and after active worker jobs finish. Successful
scheduled backups retain the configured number of automatic copies; manual
and safety copies are retained. The last managed backup cannot be deleted
through the API. Pre-existing directories without this manifest format remain
untouched and are not automatically imported as verified backups.

## Site releases

OPS rebuilds data using the installed PAUK worker and builds the installed
frontend checkout in a temporary Node container. It does not fetch code,
switch branches or deploy Python changes. Code deployment remains separate.
The resulting build must include the map's required JSON files. Only the map
entry is built; OPS is not included in a map release.

Builds are stored in `PAUK_ESSDATA/releases/<stand>/<id>/`, with `site/`, a separately retained `public/` dataset and
`release.json`. nginx serves a local copy under `~/pauk/site/<stand>/releases/`.
Precreated empty slot directories are initialized on first use; nonempty
unmanaged directories are preserved. Slot links are relative and replaced atomically: `main`, `preview` and
`archive-8522` through `archive-8529`. Published directories use 755 and files
644, including generated JSON. Builds include precompressed `.gz` assets
for nginx `gzip_static`. Reports remain nginx's separate shared alias.

The first main publication imports an existing ordinary `main/` directory
into a saved release and retains the original directory. Publication checks
`graph-data.json` through nginx. If that check fails, the previous main
release is restored. Previous builds stay available in the archive. The
rollback operation selects a saved build; it does not rebuild or restore a DB.
Archive slots are explicitly started/stopped and limited to the eight ports.

GitHub Pages always links to <https://itmo-nss-team.github.io/PAUK/>.
OPS uses `scripts/publish_pages.sh`, the repository's existing GitHub Pages
publication mechanism. An optional `PAUK_PAGES_SCRIPT` overrides that path.
The dedicated image includes `gh`; the token is passed through `GH_TOKEN`.
The script validates the four public JSON files, accepts only anonymized
sequential author keys and a whitelist of author-detail fields, uploads the
archive to the `pages-data` GitHub release and dispatches `pages.yml` on `main`.
It waits for the new workflow run and exits nonzero if deployment fails.
OPS then checks the public URL and records the actual deployed `headSha`.
The public dataset of the current managed lab release is reused, so publication
does not recalculate its layout. Legacy releases without public data generate
a fresh public dataset instead; private JSON is never used as a fallback.
No repository or release is modified during local checks.

Public data is generated with `graph_builder/public.py::to_public`, preserving
the main branch's public-site contract: masked names, remapped author IDs,
whitelisted fields and cleaned abstracts. The private variant is not used as
a fallback. `npm run build:public` selects only the map entry, relative asset
URLs and `data/gui/public`; it emits `dist-public`. The Pages workflow fetches
that public archive and never receives the OPS frontend or private JSON.

The UI asks for typed confirmation before the script runs. GitHub Pages uses
`main` for frontend code, as the existing workflow does; laboratory releases
use the installed checkout. The operation log identifies the workflow result.

## Local validation

Run `uv run python -m unittest tests.unit.test_ops` for queue durability,
anonymous reads and writes, explicit confirmations, checksum rejection, safety-copy requirements and release
rollback. The frontend's normal TypeScript, ESLint and Vitest commands cover
its contracts and desktop-only loading. Full dump/load validation needs
isolated MongoDB 4.4 and Neo4j 2026.05.0 containers:
`PAUK_TEST_OPS_RESTORE=1 uv run python -m unittest tests.integration.test_ops_restore`.
It creates a uniquely named Compose project and removes its containers and
temporary data on exit. Never use working databases as a test fixture.
