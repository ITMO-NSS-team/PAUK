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
pauk run --from 2025-01-01 --to 2025-03-31

# Arbitrary list of OpenAlex IDs
pauk run --works-file selected_works.txt --name selected-july

# Individual steps
pauk collect --work W2741809807
pauk normalize --group 2026-07-31__W2741809807
pauk enrich code_links --group 2026-07-31__W2741809807 --input selected_ids.txt --entity publications
pauk publish graph --group 2026-07-31__W2741809807
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
publication (abstract and PDF pages). A hyphenated line break is joined; a line
break without a hyphen is not. If a PDF cannot be downloaded or parsed, the
stage is marked `failed` and retried on the next run, but the abstract result
is kept.

Every prepared document has a `_processing` field with a status per stage:
`not_started`, `completed`, `completed_empty`, `not_applicable` or `failed`.
This field is not loaded into the graph.

## Graph schema

`pauk publish graph --group <group>` loads this group's prepared collections
from MongoDB into Neo4j with `MERGE`. `id` is unique for every node type;
`GitHubProfile` also has a unique `login`.

A person's `id` is the bare OpenAlex author ID (one person, one node).
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
cd pauk/gui/web && npm install && npm run dev
```

- `pauk cache export` writes a graph snapshot to `data/cache/graph_snapshot_<date>.json`.
  `--output` sets another path; `--only persons,publications,repos,departments,organizations`
  re-reads just those groups and takes the rest from the newest snapshot.
- `pauk cache inspect [path_to_snapshot] [--table T] [--sample N]` prints table
  sizes and field stats; with `--table` and `--sample` it prints N sample rows.
- `pauk gui build` reads the newest snapshot (`--cache` picks another) and writes
  the site data into `data/gui/public` and `data/gui/private` (`--out-dir` changes
  the base folder). `--seed` sets the layout seed (default 42). `authors-detail.json`,
  which holds personal fields, is written only to `private/`.
- The Vite dev server serves `data/gui/private`.

Details: [`docs/architecture/gui.md`](docs/architecture/gui.md) and
[`docs/architecture/cache.md`](docs/architecture/cache.md).
