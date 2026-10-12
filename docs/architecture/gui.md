# `pauk/gui/`: the map

**What it covers:** how a graph snapshot becomes a layout and JSON for the
site, and how the site itself is organized.

**Files:** the `pauk/gui/graph_builder/` package (`builder.py`,
`authorship.py`, `departments.py`, `layout.py`, `nodes.py`, `edges.py`,
`grants.py`, `config.py`, tests in `tests/`) and the site in
`pauk/gui/web/`.

Of the `pauk/gui` chain, only `pauk/cache/export.py` touches Neo4j;
`pauk/gui/` reads a finished snapshot from disk, not the database (see
[cache.md](cache.md)).

## `builder.py`: snapshot to layout to JSON

```bash
pauk gui build [--cache data/cache/graph_snapshot_<dd-mm-yyyy>.json] [--out-dir ...] [--seed 42]
```

By default it takes the newest snapshot from `data/cache/` and writes to
`data/gui/` (`pauk.settings.Settings.gui_dir`), into two subfolders. Every
file is written atomically through `AtomicWriter` (`pauk.storage`).

- `graph-data.json`: the summary for the map: the department table, nodes
  (`key`, `kind`, `dept`, `label`, `rank`, `gx`, `gy` plus one summary
  number) and all edges;
- `authors-detail.json`, `repos-detail.json`, `pubs-detail.json`: extended
  fields that the site loads lazily after the map.

Privacy is decided by folder, not by content. `authors-detail.json`
(personal fields) is written only to `data/gui/private/`; the other three
files go to both `public/` and `private/`, because Vite serves static files
from a single folder (`vite.config.ts::publicDir`, which points at
`data/gui/private/`). The build runs in stages, one module each:
`authorship.py` (who wrote what), `departments.py`, `layout.py`, `nodes.py`,
`edges.py`.

**External authors.** The export takes every `Person` (with `is_itmo`) and
every `AUTHORED`. A publication reaches the map if it has at least one ITMO
author, along with all its authors. External coauthors follow the same
department rule and get the same statistics, with `is_itmo: false` on the
node; the site uses it to hide them behind the "Show external authors"
filter (off by default). Only ITMO authors go into ForceAtlas2: there are
roughly three times as many external ones, and laying them out together
smeared the department clusters. An external author is placed at the
centroid of their ITMO coauthors (weighted by shared papers) with some
jitter (`layout.py::place_external_authors`), so the filter does not move the
ITMO map. Publication layout and department-to-department links also use ITMO
authors only. An external author with no ITMO publication is left out of the
graph. For a collaboration paper with more than `hyperauthor_pub_size`
authors (`config.py`), coauthor edges are built only from ITMO authors
rather than every pair, which would otherwise produce millions of pairs. A
snapshot without `is_itmo` is read as "everyone is ITMO".

**Department assignment (`departments.py`):**
- a publication gets the majority vote of its authors (only ITMO authors
  vote, since only they have `BELONGS_TO`); the full list is in `depts` and
  one primary department in `dept` for colour;
- an author gets the department of their most recent publication;
- a repository gets the majority of the departments of the publications it
  implements, else `DEVELOPED_BY`, else the majority of its ITMO
  contributors' departments.

Ties are broken by id, deliberately not by global popularity: otherwise big
departments would pull in everything contested and grow on their own.

**Repository groups (`departments.py::repo_groups`).** A department is known
for fewer than half of the repositories, so the repositories tab is coloured
by group: the department, else the owning GitHub organization, else the
`fields` of the publications it implements. An org or field group smaller
than `REPO_EDGES.group_min` does not count, and a personal account never
forms a group. A department group uses the department's own id; org and
field groups get ids after the department table
(`graph-data.json["repo_groups"]`), so department selection, regions and URLs
on the site handle them without a separate branch
(`core/data.ts::groupIdOf` and `groupsById`).

**Grants (`grants.py`).** Authors write the grant numbers in
`Publication.funding` inconsistently (`Project 075-15-2019-1896`,
`075-15-2019-1896?`, different dashes), and some are cut short (`18-19-`,
`075-15`). `add_grant_keys` adds a normalized `grant_key` to every `funding`
entry in `pubs-detail.json`. A truncated number that is a prefix of exactly
one other number merges with it; a prefix shared by several numbers (`075-15`
is a common prefix of Ministry of Science contracts) gets `null`. Programs
(`Priority 2030`, `Horizon 2020`) currently count as grants. The site builds
a grant-to-publications index from `pubs-detail.json`
(`core/data.ts::grantIndex`): a number on a publication card opens a grant
card listing its publications, with CSV export.

**FA2 layout (`layout.py::fa2_blended_layout`).** Three separate
ForceAtlas2 runs, one each for authors, publications and repositories, each
with its own proximity measure: authors use shared publications, shared
repositories and sparse "same department" edges; publications use shared
authors, with the top-K strongest links per publication; repositories use
four signals (below). All three go through `fa2_blended_layout` and
`spread_min_distance`.

FA2 runs only on the giant connected component. Disconnected components
just repel each other without bound, and after scaling the real content
collapses to a point in the middle. Everything else (small components,
singletons) is blended in afterwards, with jitter on a coarse occupancy
grid. A final `spread_min_distance` pass (using `scipy.spatial.cKDTree`)
pushes nodes apart where they are closer than a minimum distance. Check the
layout visually: the metrics do not reflect how the result looks.

ForceAtlas2 comes from `fa2_modified` with the Barnes-Hut approximation
(`layout.py::fa2_layout`), not `networkx.forceatlas2_layout`, which builds a
dense n x n distance matrix on every iteration (O(n^2) time and memory) and
does not scale to the author graph. The layout is deterministic: start
positions come from `seed` and the algorithm has no other randomness.

**Repository edges (`config.py::RepoEdges`).** A single shared publication
left most repositories with no edges, so there are four signals, each with a
weight: a shared publication (3), a shared ITMO contributor (2), a shared
ITMO coauthor of their publications (1), a shared GitHub owner (1). A group
of `k` repositories gives each pair `weight / (k - 1)`
(`co_membership_weights`); groups larger than `group_cap` are dropped, and
each repository keeps its `top_k` strongest edges. The signals that linked a
pair are recorded in `repo_edges[].via`, and the site shows them on the edge
card.

FA2 iteration counts and edge thresholds live in `config.py`; constants used
by exactly one function (`COORD_MIN`/`COORD_MAX`, `STRANDED_JITTER`/
`STRANDED_MIN_SEP`) live directly in `layout.py`.

## `pauk/gui/web/`: the site

TypeScript + Vite; the map uses Sigma.js (graphology).
`vite.config.ts::publicDir` points at `data/gui/private/`, so the data is
picked up both by `npm run dev` and by the build.

```bash
cd pauk/gui/web
npm install
npm run dev        # local
npm run build      # dist/: static files together with the data
npm test && npm run typecheck && npm run lint
```

Layers in `src/` (import order is enforced by eslint-boundaries: a layer may
import only from itself and the layers below): `contracts` (JSON shapes,
mirroring the generator) then `core` (the `Store` state, config, i18n, data
loading) then `map` (graph population, department regions) then `features`
(tabs, panel, filters, search, selection) then `app` (entry point).

There are three tabs (Authors, Publications, Repositories) plus a global
search across all kinds. There is no database health tab on the site: the
graph checks (`pauk/admin/checks.py`, run and statistics in
`pauk/admin/graph_stats.py`) are shown by the "Здоровье БД" (database
health) page of the admin panel (`pauk/admin/health.py`); see
[../admin-panel.md](../admin-panel.md). In those queries an ITMO employee is
`p.is_itmo`, not a label; `tests/unit/test_checks.py` checks every label and
relationship type in the queries against the schema.

Deployment to the lab server is described in [deploy.md](deploy.md).
