# Scripts in `scripts/`

**What it covers:** an index of one-off and helper utilities: what each does,
when it is needed, and whether it writes anything. Details live in the
scripts' own docstrings and are not repeated here.

**Files:** the whole `scripts/` directory.

General rule: a script is something that did not fit in the pipeline because
it needs a human decision (a curated list), covers a gap in a stage (a
repository the stage does not reach), or repairs data already written
(cleanup after a bug fix). Regular work is done by `pauk` from
`pauk/cli.py`, not by these files.

The "Writes" column is the one thing worth knowing before you run a script.

## Server operations

| Script | What it does | Writes |
|---|---|---|
| `deploy.sh` | Builds the site locally from the current branch, rsyncs it to the server and restarts the `screen` session that serves it. It does **not** regenerate the map data and does no `git pull` on the server; see [deploy.md](deploy.md) | server |
| `serve_static.py` | Serves the site on the server: `http.server` plus ready-made `.gz` files for browsers that accept gzip. Started by `deploy.sh` | no |
| `mongo-up.sh` | Idempotent (re)start of the `pauk-mongo` container on the server | server |
| `systemd/` | Unit files `pauk-admin.service` (admin panel) and `pauk-worker.service` (`pauk admin worker`); installation is in [../admin-panel.md](../admin-panel.md) | no |
| `snapshot_mongo.py` | Dumps prepared collections to Extended JSON, one file per collection, with a manifest and sha256 | no, read-only |

`snapshot_mongo.py` is the rollback point. Take a snapshot before any
operation that overwrites prepared data; it also prepares the input for the
rehearsal stand. Its default collection list is what the stand imports:
`rehearsal_up.sh` reads the list from the snapshot's manifest instead of
keeping its own, and refuses to start if a file is missing. A rehearsal
without `publications`, `departments` and `organizations` is not a rehearsal:
every author it meets is created anew instead of merged with an existing one.

## Rehearsal stand

| Script | What it does | Writes |
|---|---|---|
| `rehearsal_up.sh` | Brings up a throwaway copy of production: Neo4j from a dump, Mongo from `snapshot_mongo.py` snapshots | local docker |
| `rehearsal_down.sh` | Removes the stand's containers and the volume holding the restored graph | local docker |

Run these from a normal terminal: they need membership in the `docker` group,
which a process started before `usermod -aG docker` does not have.

## Manual data additions

| Script | What it does | Writes |
|---|---|---|
| `import_curated_repos.py` | Imports a human-labelled `title,repo_url` list into `Repository.publication_ids`, that is, into `IMPLEMENTS`. Two steps: `plan` only reads and saves its decision as JSON; `apply` writes exactly the reviewed plan | `apply`: Mongo |
| `verify_curated_repos.py` | Checks each row of the source CSV against reality: is there a repository node, is there a publication node, is there an edge between them | no |
| `resolve_missing_works.py` | Asks OpenAlex which work a title (absent from the graph) corresponds to; outputs a list of ids for `pauk collect --works-file` | no |
| `harvest_orphan_repos.py` | Collects the people behind chosen repositories in a targeted way, by calling `RepoPeopleStage._harvest`. The `repo_people` stage now walks every row in the collection, so this remains a manual tool for a single targeted run | Mongo |

`verify_curated_repos.py` intentionally does not reuse the matching from
`import_curated_repos.py`: a verifier that shares logic with its subject
cannot catch a bug in that logic.

A repository row is keyed by the canonical id from the GitHub response, not
by the cited link. If a row was found only under an old id (the repository was
renamed), `plan` marks it `superseded_id`, puts the old id into the winner's
`merged_ids`, and `apply` deletes the original document after writing.
Without the deletion there would be two rows for one url, exactly the
duplicate that canonical keying exists to prevent and that the graph rejects
through its uniqueness constraint.

Order for a curated list: `plan`, review the report, `apply`,
`harvest_orphan_repos.py`, `pauk publish graph`, `verify_curated_repos.py`.

The list itself is not in the repository: it is kept by whoever maintains
it, and its path is passed with `--csv`. The flag's default
(`itmo-github-repos.csv` in the repository root) assumes that file sits next
to the checkout.

## Repairing already-written data

| Script | What it does | Writes |
|---|---|---|
| `repair_implements.py` | Removes historical `IMPLEMENTS` claims if every link to the repository is classified and none has `is_relevant=true`; leaves `pending`/`failed` alone. Dry run by default, `--apply` writes | Mongo |
| `plan_author_names_repair.py` | Finds `author_names=completed` rows with empty required name parts and builds a manifest plus per-group id files (unique across groups) for a targeted `--force` rerun; does not change Mongo | only `data/reports/` |
| `prune_stranger_profiles.py` | Deletes GitHub profiles harvested only from repositories nobody implements. Dry run by default, `--apply` writes | Mongo |

## Catalogs and quality measurement

| Script | What it does | Writes |
|---|---|---|
| `build_department_catalog.py` | One-off generation of a draft catalog of ITMO units from the structure page: faculties, institutes, centers, laboratories | draft in `data/static/` |
| `export_link_labels.py` | Exports links (or whole papers) to CSV for manual labelling; the only tooling in the repository for measuring precision and recall of link extraction | no |
| `mass_launch_osa_tool.py` | Batch launch of the external `osa_tool` over a list of repositories | external tool |

`export_link_labels.py` has two modes: `links` measures precision over links
already extracted, and `papers` measures recall, which cannot be seen from the
extracted set. In `papers` mode the links PAUK found are hidden by default:
showing them to the annotator would turn an independent search into a
confirmation and inflate recall. The `pdf_urls` column in `papers` mode holds
all PDF links separated by spaces.
