# Full PAUK run (end to end)

The full cycle: collect -> enrich -> graph -> web. Do the first run on a copy
of the databases; touch production only after the copy checks out (section 7).

## Requirements

- Docker and `uv` installed.
- A clone of the repository with dependencies: `git clone <repo> pauk && cd pauk && uv sync`.
- SSH access to `<server>`.
- The file `data/static/russian_names.csv` (the staff directory; it contains
  personal data and is not stored in the repository). The `author_names` stage
  needs it, and a full enrich fails without it. Put it in `data/static/` or set
  its path in `PAUK_RUSSIAN_NAMES_FILE`.

## 0. Pre-flight

Check disk space and busy ports:

```bash
df -h
docker ps
```

Ports `27018`, `7688` and `8501` must be free. You also need free RAM for Mongo
and Neo4j (on Linux, `free -g`). API keys are checked after `.env` is set up
(section 1).

A full cycle needs `OPENALEX_API_KEY`, `GITHUB_TOKEN` and `OPENROUTER_API_KEY`
(plus `OPENROUTER_PROXY_URL` if OpenRouter is not reachable directly).

## 1. Database copy

### Take a dump

`<MONGO_URI>` is the connection string from the server's `.env`. On Windows,
run from Git Bash.

```bash
ssh <server> "docker exec pauk-mongo mongodump --uri='<MONGO_URI>' --archive --gzip" > pauk_dump.archive.gz
```

### Start local Mongo and Neo4j

```bash
docker run -d --name pauk-mongo-copy -p 27018:27017 mongo:4.4 --wiredTigerCacheSizeGB 0.5
until docker exec pauk-mongo-copy mongo --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do sleep 1; done
docker exec -i pauk-mongo-copy mongorestore --archive --gzip < pauk_dump.archive.gz

docker run -d --name pauk-neo4j-copy -p 7688:7687 \
  -e NEO4J_AUTH=neo4j/testtest \
  -e NEO4J_server_memory_heap_max__size=1G \
  -e NEO4J_server_memory_pagecache_size=512m \
  neo4j:2026.05.0
```

### Point the environment at the copy

Put the local database addresses and the keys in the repository's `.env`:

```
MONGO_URI=mongodb://localhost:27018
MONGO_DB=pauk
NEO4J_URI=bolt://localhost:7688
NEO4J_USER=neo4j
NEO4J_PASSWORD=testtest
OPENALEX_API_KEY=<key>
GITHUB_TOKEN=<key>
OPENROUTER_API_KEY=<key>
OPENROUTER_PROXY_URL=<url, if OpenRouter is not reachable directly>
```

Check that the environment points at the copy and the keys are set (addresses
are local, keys are `True`):

```bash
uv run python -c "from pauk.settings import settings as s; print('db:', s.mongo_uri, s.neo4j_uri); print('keys:', {k:bool(getattr(s,k)) for k in ['openalex_api_key','github_token','openrouter_api_key']})"
```

## 2. Collect and enrich

`testrun` in the examples is a group name you choose and reuse in every step
(`--name` sets it, `--group` refers to it). Without `--name` a name with the
date is generated; for a multi-day run, fix `--name`. Adjust `--from`/`--to`
to your run.

All at once (collect + normalize + enrich; publish is separate):

```bash
uv run pauk run --from 2025-01-01 --to 2025-03-31 --name testrun
```

Phase by phase:

```bash
uv run pauk collect --from 2025-01-01 --to 2025-03-31 --name testrun
uv run pauk normalize --group testrun
uv run pauk enrich --group testrun
```

Stages, in execution order: `persons -> departments -> code_links ->
link_relevance -> emails -> repositories -> repo_people -> dedup ->
github_match -> author_names` (plus the optional `social_graph`). One stage:
`uv run pauk enrich <stage> --group testrun`.

The `dedup` stage folds duplicates within one group. Deduplicating the whole
graph is a separate command (section 3).

## 3. Graph

```bash
uv run pauk publish graph --group testrun     # per group
uv run pauk dedup graph                         # whole graph
```

`dedup graph` takes tens of minutes on a large graph; the planning phase logs
only at its end. Merges it held back and deferred pairs go to
`data/cache/dedup_candidates_graph.jsonl`.

## 4. Update the web

```bash
uv run pauk cache export                        # -> data/cache/graph_snapshot_<date>.json
uv run pauk gui build                           # -> data/gui/{public,private}/*.json
cd pauk/gui/web && npm install && npm run dev   # locally
./scripts/deploy.sh                             # to the server, port 8501
```

After graph changes, rebuild the web with the same chain.

## 5. Verification

Graph counters:

```bash
uv run python -c "from neo4j import GraphDatabase; from pauk.settings import settings as s; d=GraphDatabase.driver(s.neo4j_uri,auth=(s.neo4j_user,s.neo4j_password)); ses=d.session(); print({l:ses.run(f'MATCH (n:{l}) RETURN count(n) AS c').single()['c'] for l in ['Publication','Repository','Person','Department']}); d.close()"
```

## 6. Catching up a partial run

What did not finish is visible from:

- groups in Mongo versus what was published to the graph;
- graph counters versus Mongo (fewer in the graph means a group is not fully
  published);
- the distribution of stage statuses (`_processing` is a field in every
  document holding the status of each stage):

  ```bash
  uv run python -c "from pymongo import MongoClient; from pauk.settings import settings as s; import collections,pprint; db=MongoClient(s.mongo_uri)[s.mongo_db]; c=collections.Counter(); [c.update({(k,(v or {}).get('status')):1 for k,v in (p.get('_processing') or {}).items()}) for p in db.persons.find({},{'_processing':1})]; pprint.pprint(dict(c))"
  ```

  Statuses: `completed` / `completed_empty` / `failed` / `not_started` /
  `not_applicable`.

Targeted catch-up:

| Situation | Command |
|---|---|
| a stage did not finish / has `failed` | `uv run pauk enrich <stage> --group <group>` (takes non-completed rows) |
| redo `completed` rows too | add `--force` |
| only specific ids | `--input ids.txt --entity <entity>` |
| group not fully published | `uv run pauk publish graph --group <group>` |
| duplicates after a top-up | `uv run pauk dedup graph` |
| graph changed, web is stale | `cache export -> gui build` |

`--entity` accepts the keys of `PreparedStore.COLLECTIONS`: publications,
persons, departments, organizations, repositories, github_profiles, repo_links.

Rows with a wrongly recorded `author_names=completed` are first planned with a
read-only script, because `Person` is global and one shared file for one group
would not cover all ids:

```bash
uv run python scripts/plan_author_names_repair.py --out data/reports/author-names-repair
```

Take a `snapshot_mongo.py` snapshot before running the commands the script
prints. Run them sequentially, then re-run the planner to confirm zero
remaining rows, publish the affected groups and rebuild cache and web.

## 7. Promote to production

After the copy checks out, repeat sections 2-5 against production: in the
repository's `.env`, restore the production database addresses (from the
server's `.env`) instead of the local ones.

Remove the copy when done:

```bash
docker rm -f pauk-mongo-copy pauk-neo4j-copy
```

## Checklist

```
[ ] 0. pre-flight: disk / RAM / ports / keys
[ ] 1. dump -> restore into local Mongo -> local Neo4j; env on the copy, verified
[ ] 2. pauk run --from ... --to ... --name testrun
[ ] 3. pauk publish graph --group testrun ; pauk dedup graph
[ ] 4. cache export ; gui build ; deploy.sh
[ ] 5. graph counters
[ ] 6. targeted catch-up if anything is missing
[ ] 7. copy verified -> repeat on production
```
