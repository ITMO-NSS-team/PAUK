# deploy/

Everything PAUK runs as, on the lab server and on a laptop: `compose.yml`
with five base services and the optional OPS service, the PAUK image (`Dockerfile`), the site's `nginx.conf`,
the log rotation rule and the list of environment variables.

| service | what | port |
|---|---|---|
| `mongo` | MongoDB 4.4 (the server's CPU has no AVX) | 27017 |
| `neo4j` | Neo4j 2026.05.0 with APOC and GDS | 7474, 7687 |
| `admin` | the admin panel (`pauk admin serve`) | 8505 |
| `worker` | runs the panel's jobs (`pauk admin worker`) | — |
| `site` | nginx: 8520 live, 8521 preview, 8522-8529 archived versions | 8520-8529 |
| `ops` | operations panel, enabled with profile `ops` | 8506 |

The PDF crawler is a separate project with its own compose and stays outside.

## Environment

Copy `deploy/.env.example` somewhere outside the repository and fill it in; on the
server it is `~/pauk/secrets/pauk.env` (mode 600, parent directory 700).
Compose reads it with `--env-file` and passes each service only its listed
variables. `MONGO_APP_USER` and `MONGO_APP_PASSWORD` are inserted into a URI;
reserved URI characters must be percent-encoded there. Root credentials
remain the original values passed to the Mongo image. The server
account uses `sudo docker`; locally use whichever Docker access is configured.

The worker accepts the model, person resolution, concurrency and request
timeout settings listed in `.env.example`. `PAUK_PERSON_RESOLUTION_ENABLED=false`
disables person resolution. The logreg model path is inside the container;
to use a custom file, place it under `$PAUK_HOME/private/` and set the path
to `/private/<filename>`. The data, reports and Russian names paths are fixed
to their container mounts in `compose.yml`.

## Folders

Everything a container keeps lives on the host, in two roots:

```
$PAUK_HOME (local disk)        $PAUK_ESSDATA (NFS on the server)
  db/mongo/                      pdf/<stand>/
  db/neo4j/{data,plugins}/       snapshots/<stand>/
  logs/<service>/                runs/<stand>/{audit,osa}/
  private/russian_names.csv      reports/<stand>/
  gui/<stand>/                   releases/<stand>/
  site/<stand>/{main,preview}/
```

The PAUK containers run as `PAUK_UID:PAUK_GID`, so these folders must be
writable by that user. Mongo and Neo4j run as their own users inside the
image; their `db/` and `logs/` folders are made writable for them once,
when the host is set up. Database folders stay on local storage.

Admin and worker share `runs/<stand>/audit/` at `/app/data/audit`. The admin
needs this writable mount even to read the graph: its audited client creates
the JSONL audit directory during initialization. Without it, the overview
can report an unavailable graph even when Neo4j itself is reachable.

After obtaining the audit mount fix in the server checkout, run:

```bash
bash deploy/update_admin_audit.sh
```

The script uses `~/pauk/secrets/pauk.env` by default (or the path in its first
argument), builds the admin image, checks audit write access under the configured
UID/GID, and recreates only admin. It then checks graph counts through the
application's audited client. If the write check fails, correct the host audit
directory permissions for `PAUK_UID:PAUK_GID` before retrying. It does not restart
worker or databases, or update the checkout itself.

The worker's uv cache uses the Compose volume `uv-cache`, mounted at
`/home/pauk/.cache/uv`; the host's `~/.cache/uv` is not mounted. The server
project is named `deploy`. Changing that name also selects a different cache
volume and network.

Before the first OSA run, ensure this volume is writable by `PAUK_UID:PAUK_GID`.
Docker can populate a new volume with root-owned uv files from the built image.
Correct their ownership once for the configured worker user; otherwise `uvx`
fails with a cache permission error even while the worker reports healthy.
On the server, the configured user is `1004:1004`:

```bash
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml exec --user 0 worker chown -R 1004:1004 /home/pauk/.cache/uv
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml exec worker uvx --from osa_tool==0.3.2 osa-tool --help
```

Use the actual configured UID/GID on other hosts. Installing OSA explicitly
uses the worker's normal network environment; `OsaRunner` adds the LLM proxy
when generating reports. This avoids downloading the first environment through
that proxy.

## Commands

From the repository root (`~/pauk/code` on the server), with
`ENV=~/pauk/secrets/pauk.env`. Prefix Docker commands with `sudo` on the server:

```bash
docker compose --env-file $ENV -f deploy/compose.yml up -d --build   # start or update
docker compose --env-file $ENV -f deploy/compose.yml ps              # health of every service
docker compose --env-file $ENV -f deploy/compose.yml logs --since 30m
docker compose --env-file $ENV -f deploy/compose.yml restart worker
docker compose --env-file $ENV -f deploy/compose.yml down            # stop; data stays in the folders
```

`up -d --build` after a `git pull` rebuilds the PAUK image and restarts what
changed. Update the checkout and deploy only with approval. The worker gets
five minutes to finish the job in hand, so prefer an idle queue. `restart`
does not pick up code changes from a rebuilt image; use `up -d --build`.

`healthy` checks do not verify map JSON, reports, full pipeline execution or
OSA. Check the login page, `/graph-data.json`, `/reports/index.json` and a PDF
separately. Site publication is described in
[deploy.md](../docs/architecture/deploy.md#static-site-and-reports).

## Logs

Each service writes to `$PAUK_HOME/logs/<service>/`. PAUK's own lines share
one layout (time, service, level, module, `[job]`, message), so all errors
across services are one command:

```bash
grep -rh " ERROR " ~/pauk/logs | sort | tail -50
```

Install `logrotate.conf` for host-side rotation:

```bash
sudo cp deploy/logrotate.conf /etc/logrotate.d/pauk
```

`size 20M` triggers rotation when logrotate runs; it is not a hard size cap.
Three rotated copies are kept per file. The host timer runs daily.
`copytruncate` handles Mongo and nginx keeping files open. Docker also keeps
one JSON output file up to 10 MB per container for `docker compose logs`.

## On a laptop

Point `PAUK_HOME` and `PAUK_ESSDATA` at local folders, lower the memory
(`MONGO_CACHE_GB=1`, `NEO4J_HEAP=512m`, `NEO4J_PAGECACHE=256m`) and set
`PAUK_UID`/`PAUK_GID` to your own. On an empty database there is only the
root user: either create the app user or put the root credentials into
`MONGO_APP_USER`/`MONGO_APP_PASSWORD`.

## OPS

The optional `ops` profile builds the control plane without authentication and its
frontend as a separate image. It requires Compose support for
`additional_contexts` and the existing `admin` image as its base. Once code
changes and deployment have been approved:

```bash
sudo docker compose --env-file /home/asteb/pauk/secrets/pauk.env -f deploy/compose.yml --profile ops up -d --build admin worker ops
```

Open `http://einsteinium.nsslab:8506/ops/` directly, without a login.
Anyone with network access to this port can manage OPS.
Only OPS receives the Docker socket; the graph editor and public nginx do not.
OPS uses `restart: unless-stopped`. Its state is stored at `PAUK_HOME/ops`,
independently of Mongo/Neo4j and site releases. Enable automatic backup
maintenance only after reviewing its interruption window in the UI.

The default public-site publisher is `scripts/publish_pages.sh` from the
mounted checkout. `PAUK_PAGES_SCRIPT` can override it. The image includes `gh`;
`GITHUB_TOKEN` needs release-write and workflow-dispatch permissions. The
publisher uploads only validated public data and waits for the existing Pages
workflow to finish. The map's public build never uses private JSON.
See [../docs/architecture/ops.md](../docs/architecture/ops.md) for contracts,
recovery behavior and limitations. No server is changed by merely building
the optional image locally.
