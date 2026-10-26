# deploy/

Everything PAUK runs as, on the lab server and on a laptop: `compose.yml`
with five services, the PAUK image (`Dockerfile`), the site's `nginx.conf`,
the log rotation rule and the list of environment variables.

| service | what | port |
|---|---|---|
| `mongo` | MongoDB 4.4 (the server's CPU has no AVX) | 27017 |
| `neo4j` | Neo4j 2026.05.0 with APOC and GDS | 7474, 7687 |
| `admin` | the admin panel (`pauk admin serve`) | 8505 |
| `worker` | runs the panel's jobs (`pauk admin worker`) | — |
| `site` | nginx: 8520 live, 8521 preview, 8522-8529 archived versions | 8520-8529 |

The PDF crawler is a separate project with its own compose and stays outside.

## Environment

Copy `.env.example` somewhere outside the repository and fill it in; on the
server it is `~/pauk/secrets/pauk.env` (mode 600). Compose reads it with
`--env-file` and passes each service only the variables listed for it.

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
when the host is set up.

## Commands

From the repository root, with `ENV=~/pauk/secrets/pauk.env`:

```bash
docker compose --env-file $ENV -f deploy/compose.yml up -d --build   # start or update
docker compose --env-file $ENV -f deploy/compose.yml ps              # health of every service
docker compose --env-file $ENV -f deploy/compose.yml logs --since 30m
docker compose --env-file $ENV -f deploy/compose.yml restart worker
docker compose --env-file $ENV -f deploy/compose.yml down            # stop; data stays in the folders
```

`up -d --build` after a `git pull` rebuilds the PAUK image and restarts what
changed. The worker gets five minutes to finish the job in hand.

## Logs

Each service writes to `$PAUK_HOME/logs/<service>/`. PAUK's own lines share
one layout (time, service, level, module, `[job]`, message), so all errors
across services are one command:

```bash
grep -rh " ERROR " ~/pauk/logs | sort | tail -50
```

The size limit (3 files of 20 MB) is `logrotate.conf`:

```bash
sudo cp deploy/logrotate.conf /etc/logrotate.d/pauk
```

Docker keeps a small copy of each container's output as well, for
`docker compose logs`.

## On a laptop

Point `PAUK_HOME` and `PAUK_ESSDATA` at local folders, lower the memory
(`MONGO_CACHE_GB=1`, `NEO4J_HEAP=512m`, `NEO4J_PAGECACHE=256m`) and set
`PAUK_UID`/`PAUK_GID` to your own. On an empty database there is only the
root user: either create the app user or put the root credentials into
`MONGO_APP_USER`/`MONGO_APP_PASSWORD`.
