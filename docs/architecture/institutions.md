# University profiles (`pauk/institution.py`)

**What this covers:** how one codebase builds the graph of more than one
university, what a profile holds, what each university has of its own on disk
and in the databases, and what to do to add another one.

**Files involved:** `pauk/institution.py`, `pauk/settings.py`,
`data/static/<institution>/`, `pauk/gui/web/src/core/i18n.ts`,
`pauk/gui/web/vite.config.ts`, `scripts/deploy.sh`,
`scripts/migrate_local_names.py`.

## Choosing the university

`PAUK_INSTITUTION` (`itmo` by default, `ufjf`, or the key of any
`data/static/<key>/profile.json`) picks the profile for the whole process;
`Settings.institution` returns it. Nothing outside `pauk/institution.py` names
a university: code asks the profile. ITMO and UFJF are written out in Python;
every other university is a `profile.json` with the same fields
(`institution.load`, which refuses an unknown field so that a typo does not
pass silently).

| | ITMO | UFJF |
|---|---|---|
| ROR | `04txgxn49` | `04yqw9c44` |
| local language (`*_local` fields) | Russian, Cyrillic | Brazilian Portuguese, Latin |
| patronymic (`second_name_*`) | yes | never |
| staff catalog | `russian_names.csv` (personal data, not committed) | none |
| email domains | `itmo.ru`, `ifmo.ru` | `ufjf.br`, `ufjf.edu.br` (and subdomains) |
| MongoDB database | `pauk` | `pauk_ufjf` |
| external coauthors in the graph | yes | no |
| site on the lab server | `~/pauk-gui`, port 8501 | `~/pauk-gui-ufjf`, port 8502 |

What the profile drives:

- **who is a member** (`is_member`): an OpenAlex authorship whose institution
  carries the profile's ROR, or an ORCID employment with it
  (`Institution.is_member_ror`). Only the main ROR counts. UFJF's university
  hospital (HU-UFJF, ROR `0039g6563`, run by EBSERH) is left out on purpose:
  its works are not collected and its staff are members only through a UFJF
  authorship of their own;
- **collection**: `pauk collect` filters OpenAlex works by the profile's ROR;
- **departments**: the marker, the spelled-out name, the address words and the
  neighbouring organisations used by the `departments` stage
  ([pipeline/departments.md](pipeline/departments.md));
- **names**: the language, alphabet and patronymic rule of the `author_names`
  stage ([pipeline/author-names.md](pipeline/author-names.md));
- **emails and GitHub**: the domains that mark a university address
  (`emails`, `github_match`), the words and the city that mark a GitHub profile
  (`github_match`, `social_graph`);
- **the admin panel**: checks about Russian names (patronymic, transliteration,
  mixed alphabets) are shown only for a Cyrillic profile;
- **the site**: the university's name in the interface.

## Local names: `*_local`

`name_local`, `surname_local`, `first_name_local` and `second_name_local` hold a
name in the university's own language; `*_en` hold English. For ITMO they are
Russian (they were called `*_ru` before profiles existed), for UFJF
Portuguese. The site's "RU" interface shows the local form, the "EN" one the
English form.

A graph built before the rename carries `name_ru`, `surname_ru`, ... and
`is_itmo`. `scripts/migrate_local_names.py` renames them in place, in MongoDB
(prepared rows, revision snapshots, pre-merge snapshots, manual decisions)
and in Neo4j. It is a dry run without `--apply`; take `snapshot_mongo.py`
first.

## What each university has of its own

- **Static data**: `data/static/<institution>/departments_catalog.json` and
  `github_orgs.json` (`Settings.static_dir`). The staff catalog, where there is
  one, lies next to them and is never committed.
- **Outputs**: snapshots in `data/cache/<institution>/`, site data in
  `data/gui/<institution>/{public,private}/`, journals in
  `data/audit/<institution>/`. PDFs (`data/pdf/`) are keyed by work and shared.
- **MongoDB**: one database per university (`Institution.mongo_db`, overridden
  by `MONGO_DB`). Entities are global inside a database, so two universities in
  one would merge each other's people.
- **Neo4j**: one instance per university (`NEO4J_URI`). `pauk dedup graph`
  reads the whole graph, and Community edition has a single database.

## Prototype decisions (2026-10-21)

Made while building prototype graphs for the article; each changes ITMO too,
and the ICDM demo's figures (12,814 publications, 9,117 staff) were computed
before them. To go back for ITMO, revert or switch the item; the branch
started from `8cbf2a8`.

| Decision | Where | To restore the earlier behaviour |
|---|---|---|
| Membership: university ROR on an authorship, or an ORCID employment; the OpenAlex author profile no longer counts | `persons.is_member` | the old rule counted any `Person.affiliations` entry with the ROR |
| Crossref ORCID backfill off | `Settings.crossref_enabled` | `PAUK_CROSSREF_ENABLED=true` |
| External coauthors not enriched, not deduplicated among themselves, not published | `Institution.external_authors` (true for ITMO only) | set it true in the university's profile |
| `author_names` and `link_relevance` on `anthropic/claude-haiku-5.5`, Russian names included, although it did worse on them (see [pipeline/author-names.md](pipeline/author-names.md)) | `PAUK_LLM_MODEL` | `PAUK_LLM_MODEL=anthropic/claude-haiku-4.5` |
| Organisations with their own ROR (medical centres, affiliated hospitals, partner colleges) are neither members nor catalogue units; ROR "child" units (MIT CSAIL) stay in catalogues | membership by ROR; `scripts/draft_institution.py::drop_related_organisations` | — |
| One Neo4j instance per university, built on one laptop | `scripts/run_institutions.py` | — |
| Universities with more than 30,000 works a year left out | the list in `data/reports/institutions/` | — |

## Moving an existing ITMO deployment

Profiles moved a few things an ITMO deployment already has. Once, on every
machine that runs the pipeline or the panel:

1. `snapshot_mongo.py`, then `uv run python scripts/migrate_local_names.py`
   (dry run) and the same with `--apply`.
2. The staff directory from `data/static/russian_names.csv` to
   `data/static/itmo/russian_names.csv` (or point `PAUK_STAFF_CATALOG_FILE`
   at it; the old `PAUK_RUSSIAN_NAMES_FILE` is still read).
3. Snapshots from `data/cache/` to `data/cache/itmo/`, site data from
   `data/gui/{public,private}/` to `data/gui/itmo/`, journals from
   `data/audit/` to `data/audit/itmo/`, or rebuild them with
   `pauk cache export` and `pauk gui build`.
4. Restart the admin panel and the worker.

`MONGO_DB` and the ITMO site's folder, session and port stay as they were.

## The UFJF department catalogue

Built by hand on 2026-10-21 from the university's list of academic units
(`www2.ufjf.br/ufjf/sobre/organizacao/unidadesacademicas/`), each unit's page of
departments, and the affiliation strings of 21,480 UFJF works in OpenAlex
(2020 onward), which show how authors actually write the units.

- 6 institutes, 15 faculties and the Colégio de Aplicação João XXIII hang off
  the root `ufjf`; the Governador Valadares campus (`campus-gv`) is a unit of
  its own with its two institutes (ICV, ICSA) below it.
- 61 departments. The departments of Direito, Educação and Comunicação, and
  those of ICSA (whose names repeat Juiz de Fora's), are not listed: their pages
  do not name them, or the names say nothing about the campus.
- Two graduate programs are units of their own because authors sign with them
  far more than with a department: "Department of Electrical Engineering" is the
  electrical engineering program (`eng-ppee`), not a department UFJF has, and
  computational modelling (`pgmc`) belongs to no single faculty. Other graduate
  programs are aliases of the unit that runs them.
- Almost every official name is generic ("Departamento de Química" exists at
  every university), so names are context aliases: they match only next to
  UFJF. The plain aliases are forms that name UFJF by themselves ("ICB/UFJF",
  "UFJF-GV", "FAEFID").

On that sample the matcher assigns a unit to 44.7% of UFJF authors. About 56%
write nothing but the university, so of the authors that name a unit roughly
three in four are matched. 4 of 31,491 external authors are matched, each
through a former UFJF affiliation in their biography.

## Adding a university

1. Draft the profile and the catalogue: `uv run python
   scripts/draft_institution.py <key> <ror>`. It reads the names, acronyms,
   domains and city from ROR and a year of the university's works from
   OpenAlex, asks an LLM (`claude-opus-5.5` by default) for
   `data/static/<key>/profile.json` and `departments_catalog.json`, runs the
   matcher with the draft over the same works, gives the model that report
   once to revise, and writes the final report to
   `data/reports/institutions/<key>.md`: member authors matched, external
   authors matched, units never hit, frequent unmatched member phrases.
2. Review the report and the catalogue by hand. The draft is the model's
   reading of how members write their affiliations; units it is unsure of
   should be checked on the university's site, as was done for UFJF.
3. `unit_words` in the profile: where the university's own units carry a word
   that otherwise names a separate organisation ("College of Computer
   Science" at Fudan, the colleges of Cambridge), it keeps those parts from
   being read as a neighbour.
4. `github_orgs.json` if the university has GitHub organizations whose login
   does not name it.
5. Only if the map is built for it: the university's short name in
   `INSTITUTION_NAMES` (`pauk/gui/web/src/core/i18n.ts`).
6. If its names are in neither Cyrillic nor Latin script, `author_names` needs
   a prompt for that script; for Chinese, Japanese and Korean universities the
   drafts take names in Latin script.
7. A run on real data (see AGENTS.md): the department coverage above came from
   the matcher run over the university's OpenAlex affiliations, and so should
   the next one.
