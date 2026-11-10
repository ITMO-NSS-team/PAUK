# Deploying to the lab server

**What it covers:** the running services, host storage, operational checks and
site publication. Compose configuration and environment setup are in
[`deploy/README.md`](../../deploy/README.md).

**Files:** `deploy/compose.yml`, `Dockerfile`, `nginx.conf`, `logrotate.conf`
and `.env.example` in `deploy/`.

## Access and services

Connect over the lab VPN with `ssh asteb@einsteinium.nsslab`. Run remote
commands in a login shell (`ssh asteb@einsteinium.nsslab 'bash -l -s'`).
The account needs `sudo` for Docker; commands below use `sudo docker`.

The checkout is `/home/asteb/pauk/code`. Docker CE runs six long-running Compose services
with `restart: unless-stopped`; no PAUK systemd unit or screen session is
needed. Docker's own system service starts the containers after reboot.

| Service | Entry point | Host port |
|---|---|---|
| `mongo` | MongoDB 4.4 with authentication | 27017 |
| `neo4j` | Neo4j 2026.05.0, APOC and GDS | 7474, 7687 |
| `admin` | `pauk admin serve` | 8505 |
| `worker` | `pauk admin worker` | none |
| `site` | nginx, static site and shared reports | 8520–8529 |
| `ops` | FastAPI control plane and its built frontend | 8506 |

Live site: `http://einsteinium.nsslab:8520`. Admin login:
`http://einsteinium.nsslab:8505/login`. Port 8521 serves `preview/`;
8522–8529 serve `archive-<port>/`. Empty slots return 404. The ports and
folders are managed by OPS at `http://einsteinium.nsslab:8506/ops/`. The
`ops` Compose profile is enabled on this deployment. OPS has no login; anyone
with network access to port 8506 can manage operations. See [ops.md](ops.md).

The Compose project is `deploy`, so resources have names such as
`deploy-worker-1` and `deploy_uv-cache`. Use service names with `compose exec`
and `compose logs`, rather than hard-coded container names. Changing the
project name creates a separate set of resources, including the OSA cache.

## Storage layout

Diagram: [`../diagrams/server-storage.md`](../diagrams/server-storage.md).
`PAUK_HOME=/home/asteb/pauk` is on the local ext4 disk;
`PAUK_ESSDATA=/var/essdata/pauk` is on NFS4 backed by GPFS. Databases use local
bind mounts: synchronous database writes do not belong on the network share.
Do not infer a writable quota from `df` free space on the share.

| Host path | Purpose / container path |
|---|---|
| `~/pauk/code/` | checkout used to build the PAUK image |
| `~/pauk/ops/` | durable OPS journal, settings, operation logs and deployment metadata |
| `~/pauk/db/mongo/` | MongoDB `/data/db` |
| `~/pauk/db/neo4j/data/` | Neo4j `/data` |
| `~/pauk/db/neo4j/plugins/` | APOC and GDS jars, mounted at `/plugins` |
| `~/pauk/secrets/pauk.env` | Compose configuration and secrets, mode 600; parent 700 |
| `~/pauk/private/` | staff catalog, worker `/private`, read-only mount |
| `~/pauk/logs/<service>/` | service logs |
| `~/pauk/gui/itmo/` | generated map data, worker `/app/data/gui` |
| `~/pauk/site/itmo/` | published builds, nginx `/srv/site`, read-only mount |
| `/var/essdata/pauk/pdf/itmo/` | worker `/app/data/pdf` |
| `/var/essdata/pauk/snapshots/itmo/` | worker `/app/data/cache` |
| `/var/essdata/pauk/runs/itmo/audit/` | worker `/app/data/audit` |
| `/var/essdata/pauk/runs/itmo/osa/` | OSA clones and logs, worker `/app/data/osa` |
| `/var/essdata/pauk/reports/itmo/` | worker `/reports`, nginx `/srv/reports` |
| `/var/essdata/pauk/dumps/<stand>/` | Mongo archives and Neo4j database dumps |
| `/var/essdata/pauk/releases/<stand>/` | stored site builds and public datasets |
| `/var/essdata/pauk/deployments/<id>/` | source archive and manifest for deployment rollback |

The worker's uv cache is the Docker volume `deploy_uv-cache`, mounted at
`/home/pauk/.cache/uv`. It is independent of the host's `~/.cache/uv`.
The cache must be owned by the configured worker UID/GID. A newly created
volume can inherit root-owned files from the image; check write access as the
worker before running OSA.
`itmo` is the active stand; other datasets have their own directories.
Secrets and the staff catalog stay on the local disk.

MongoDB is pinned to 4.4 because the host CPU lacks AVX. The cache is set to
6 GB. Neo4j uses a 2 GB initial/max heap, a 1 GB page cache, `CYPHER_25`,
and the mounted APOC/GDS jars. PAUK services run under `PAUK_UID:PAUK_GID`;
MongoDB and Neo4j use their image users.

## Operation and checks

From `~/pauk/code`:

```bash
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml ps
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml logs --since 30m
sudo docker stats --no-stream
df -h / /var/essdata
curl -fsS http://127.0.0.1:8505/login -o /dev/null
curl -fsS http://127.0.0.1:8520/graph-data.json -o /dev/null
curl -fsS http://127.0.0.1:8520/reports/index.json -o /dev/null
```

`healthy` is a narrow probe: Mongo answers ping, Neo4j accepts a Bolt TCP
connection, admin serves the login page, worker can reach Mongo, nginx
serves `/`. It does not prove that a graph query, worker job, JSON asset or
OSA report works. Check those separately. Inspect restart counts and
`OOMKilled` with `docker inspect` when investigating a service interruption.

Read `jobs.state` and the latest `job_workers.seen_at` in Mongo to distinguish
an idle worker from an unresponsive one. A health-check job exercises the
graph, not a full enrichment or OSA run.

The host's logrotate timer processes `/etc/logrotate.d/pauk`, installed from
`deploy/logrotate.conf`. Rotation is checked daily: `size 20M` is a trigger
at that check, not a hard cap. It retains three rotated copies per file.
`copytruncate` supports MongoDB and nginx keeping files open. Docker keeps
one additional JSON log file up to 10 MB per container. PAUK logs redact
configured secrets and include service, level, module and the current job tag.

Deploy code changes only after approval: update the checkout, then run
`sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml up -d --build`.
The worker gets five minutes to finish its current job before Docker kills
it. Prefer updating while the queue is idle. Rebuilding the image does not
build or publish the static site.

## Backups

Database backups are kept under `/var/essdata/pauk/dumps/<stand>/`.
A full database backup contains a Mongo `mongodump --archive --gzip` archive
and separate Neo4j dumps for `neo4j` and `system`. The latter carries database
users and permissions. `snapshot_mongo.py` exports selected prepared
collections and is not a substitute for this backup.

The optional OPS service automates full backups and restores using a
persistent journal outside the databases; see [ops.md](ops.md). Creating
an offline Neo4j dump requires stopping Neo4j, the editor and worker in an
approved maintenance window. Keep existing backups until restoration has been checked; archive
presence and file size alone do not prove recoverability.

## Static site and reports

`pauk cache export` and `pauk gui build` produce map data. The worker mounts
that output at `~/pauk/gui/itmo`; nginx serves a separate built directory at
`~/pauk/site/itmo/main`. A map rebuild does not update the published site.

Build with `npm run build` in `pauk/gui/web/`. Vite copies
`data/gui/private/` into `dist/`, including personal author details, so publish
this build only within the lab VPN. Copy the reviewed build into the desired
site slot. Publication and replacement of a live slot require approval.
The base PAUK image contains no Node.js or frontend builder. OPS builds the
map in a temporary Node container and has its own separate frontend image.

Published directories must be traversable by nginx and published files must
be readable by its worker user: normally directories 755 and files 644.
`AtomicWriter` creates JSON files with mode 600. Copying them without adjusting
permissions can leave the HTML responding 200 while map data returns 403.
Normalize permissions on published assets and on `reports/index.json` after
regeneration; do not change permissions on secrets or source catalogs.

Reports are shared across slots through nginx's `/reports/` alias. Run
`pauk reports build` explicitly in the worker container; it is not part of a
map rebuild or a queued pipeline job. `OPENROUTER_PROXY_URL` enables OpenRouter
access from the server; OSA environments are installed by `uvx` in the worker's
cache. See [reports.md](reports.md). nginx serves prebuilt `.gz` files through
`gzip_static`; compression must be done as part of preparing the build.

## External PDF crawler

`~/PDF-Crawler-Service` is a separate checkout with its own Compose project
(API, Celery worker and Redis). Its API is published on port 8000. It is not
managed by PAUK's Compose file. PAUK leaves `PAUK_PDF_CRAWLER_URL` empty, so
its fallback is disabled even when the external service is running.

When enabled, the code-links stage probes `GET {url}/health` once per stage
and requests `GET {url}/download?url=<doi>` with a 180-second timeout and no
retries. Inside the worker, a host service is reached through
`host.docker.internal`, not `localhost`. See
[pipeline/code-links.md](pipeline/code-links.md).
