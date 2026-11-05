# PAUK

PAUK collects publications from OpenAlex, enriches them with data from external sources (Crossref, ORCID, GitHub), finds code links in them, and loads the result into Neo4j.

## Data

```text
data/static/                 # versioned reference data, including departments_catalog.json
MongoDB: raw                 # full immutable API responses, per group
MongoDB: publications/persons/departments/organizations/repositories/github_profiles/repo_links
                             # prepared entities for Neo4j, global (not per group)
```

## Usage

```bash
# One OpenAlex work
pauk run --work W2741809807

# Publications for a period
pauk run --from <start-date> --to <end-date>

# Arbitrary list of OpenAlex IDs
pauk run --works-file selected_works.txt --name selected-july

# Individual steps
pauk collect --work W2741809807 --name example-work
pauk normalize --group example-work
pauk enrich code_links --group example-work --input selected_ids.txt --entity publications
pauk publish graph --group example-work
```

`enrich --group` is always required; `--input <file> --entity <entity>`
further narrows the run to the ids listed in the file (one per line). A
repeated `collect` does not add OpenAlex works that are already stored, and a
repeated `normalize` keeps enrichment data, even when the same work appears in
another, overlapping group: entities in MongoDB are global, not per group.
`enrich --force` reprocesses rows with status `completed` too (for example,
after fixing a stage).

Code links are extracted from the abstract and, when `pdf_urls` exist (or a PDF
is found by DOI via `PAUK_PDF_CRAWLER_URL`, see `.env.example`), from the PDF
page by page (`pauk enrich code_links`; PDFs are cached in `data/pdf/`). Both
bare mentions like `github.com/org/repo` and real hyperlinks are found. The PDF
text is saved in `Publication.full_text`.

`context` and `page_number` on `MENTIONS_LINK` are lists of occurrences per
publication (abstract and PDF pages). Hyphenated breaks and breaks after underscores preserve alternative URL
readings. A continuous spelling in the same PDF can resolve the ambiguity;
other line breaks are not joined. If a PDF cannot be downloaded or parsed, the
stage is marked `failed` and retried on the next run, but the abstract result
is kept.

Every prepared document has a `_processing` field with a status per stage:
`not_started`, `completed`, `completed_empty`, `not_applicable` or `failed`.
This field is not loaded into the graph.

## Graph schema

`pauk publish graph --group <group>` loads this group's prepared collections
from MongoDB into Neo4j with `MERGE`. `id` is unique for every node type;
`GitHubProfile` also has a unique `login`.

A person's `id` is normally the bare OpenAlex author ID. Records without it
use a deterministic ORCID- or name-based fallback until dedup resolves them.
`is_itmo` is set to `true` if at least one work has an ITMO affiliation, and
data from other groups never lowers it back to `false`. Repositories whose
`repositories` stage ended with status `failed` (for example, a 404) are not
loaded until a successful retry; their links remain `LinkCandidate` nodes.

| Node | Neo4j label | Main properties |
|---|---|---|
| Department | `Department` | `id`, `name_en`, `name_ru`, `name_variants` |
| Organization | `Organization` | `id`, `name_en` |
| ITMO employee | `Person {is_itmo: true}` | `id`, `openalex_id`, `orcid`, full name, contacts, profiles |
| External author | `Person {is_itmo: false}` | `id`, `openalex_id`, `orcid`, `name_raw`, `name_variants`, `email` |
| Publication | `Publication` | `id`, `title`, `doi`, date, journal, code, funding, OpenAlex/PDF URLs, abstract |
| Repository | `Repository` | `id`, `name`, `url`, description, stars, license, dates |
| GitHub profile | `GitHubProfile` | `id`, `login`, `name`, URL, description, location, type |
| Link candidate | `LinkCandidate` | `id` (the URL), `url`, `host` |

Relationships:

```text
(:Person {is_itmo: true})  -[:BELONGS_TO]->  (:Department)
(:Person {is_itmo: true})  -[:AUTHORED]->    (:Publication)
(:Person {is_itmo: false}) -[:AUTHORED]->    (:Publication)
(:Person {is_itmo: true})  -[:CONTRIBUTED_TO]-> (:Repository)

(:Department)  -[:PART_OF]->       (:Department | :Organization)

(:Publication) -[:PRODUCED_BY]->   (:Department)
(:Publication) -[:MENTIONS_LINK]-> (:Repository | :LinkCandidate)

(:Repository) -[:DEVELOPED_BY]-> (:Department)
(:Repository) -[:IMPLEMENTS]->   (:Publication)
(:Repository) -[:OWNED_BY]->     (:GitHubProfile)
```

`AUTHORED` keeps `position`, `affiliation` and `is_corresponding`;
`CONTRIBUTED_TO` keeps `role`; `MENTIONS_LINK` keeps the context, page number
and link-check result. The internal `_processing` field and any fields not
listed in `pauk/graph/extract.py` do not reach Neo4j.

`MENTIONS_LINK` means the link occurred in the publication, regardless of the
check result. `IMPLEMENTS` is created only for a repository classified as the
authors' own result for that publication.

Full details: [`docs/architecture/neo4j-graph.md`](docs/architecture/neo4j-graph.md).

## GUI

The visualization lives in `pauk/gui`. After the data is in Neo4j, snapshot the
graph, build the site data, and start the dev server:

```bash
pauk cache export
pauk gui build
pauk reports build   # optional
cd pauk/gui/web && npm install && npm run dev
```

- `pauk cache export` writes a graph snapshot to `data/cache/graph_snapshot_<date>.json`.
  `--output` sets another path; `--only persons,publications,repos,departments,organizations`
  re-reads just those groups and takes the rest from the newest snapshot.
- `pauk cache inspect [path_to_snapshot] [--table T] [--sample N]` prints table
  sizes and field stats; with `--table` and `--sample` it prints N sample rows.
- `pauk gui build` reads the newest snapshot (`--cache` picks another) and writes
  the site data into `data/gui/public` and `data/gui/private` (`--out-dir` changes
  the base folder). `--seed` sets the layout seed (default 42). The public variant
  masks author names, remaps author IDs and contains only whitelisted fields;
  personal author fields stay in the private variant.
- `pauk reports build [<repo id> ...]` runs [OSA](https://github.com/aimclub/OSA)
  on every repository of the newest snapshot that has no report yet, except
  repositories only cited as third-party code, and writes
  `data/gui/private/reports/<lang>/<repo id>.pdf` (`en` and `ru`) plus
  `reports/index.json`; the repository card links the report in the interface
  language. `--force` regenerates existing reports, `--workers` sets the
  parallel runs (default 6). It needs `OPENROUTER_API_KEY` and `uv`; when every
  report exists it only rebuilds the index, offline. `PAUK_REPORTS_DIR` moves
  the PDFs elsewhere.
- The Vite dev server serves `data/gui/private`.
- `npm run build:public` builds GitHub Pages from `data/gui/public` into
  `dist-public`. `scripts/publish_pages.sh` validates public data, uploads it
  and waits for the existing Pages workflow; running it publishes to GitHub.

Details: [`docs/architecture/gui.md`](docs/architecture/gui.md),
[`docs/architecture/cache.md`](docs/architecture/cache.md) and
[`docs/architecture/reports.md`](docs/architecture/reports.md).

## Server deployment

The lab server runs Mongo, Neo4j, admin, worker, nginx and OPS from
`deploy/compose.yml`. The live site is on port 8520, the admin panel on 8505,
and OPS on 8506 with the `ops` profile enabled.
Map-data generation and site publication are separate steps. See
[deployment](docs/architecture/deploy.md) and
[Compose setup](deploy/README.md).

The optional `ops` Compose profile provides the operations panel without authentication
on port 8506: service status, full backups and restore, site releases, GitHub
Pages and persistent operation logs. Its own frontend is built without map
data. Setup and recovery behavior: [OPS](docs/architecture/ops.md).
