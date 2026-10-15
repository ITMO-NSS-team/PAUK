# `pauk/pipeline/`: orchestration

**What this covers:** how collection, normalization and the enrichment stages
fit together, and how a run resumes.

**Files involved:** `pauk/pipeline/collect.py`, `normalize.py`, `enrich.py`,
`runner.py`, `selectors.py`, `stages/base.py`, `stages/__init__.py`.

Three CLI commands, each a separate step (see [../cli.md](../cli.md)):
`collect`, `normalize`, `enrich [stage]`. `dedup` runs inside `enrich` as one
of the stages. `pauk run` chains all three under a per-group lock held by
`PipelineRunner`; `Enricher` itself takes no lock. `publish graph` and
`dedup graph` live in `pauk/graph/`, not here.

## `collect.py::Collector`

Fetches raw works from OpenAlex into MongoDB (collection `raw`, source
`openalex_works`, see [../storage.md](../storage.md)). Three selectors
(`pauk/pipeline/selectors.py`): `WorkSelector` (one id), `WorksFileSelector`
(a file of ids, one per line) and `PeriodSelector` (a date range, cursor
pagination by ITMO's ROR id, `ITMO_ROR_ID = "04txgxn49"`).

**Truncated author lists.** The OpenAlex list endpoint returns at most 100
authorships per work and does not always flag the cut. `_authors_truncated()`
treats a payload as truncated if `is_authors_truncated` is set or the list
length equals the limit. Such records are re-fetched through the single-work
endpoint, whose author list is always complete. `refetch_truncated()` runs at
the start of every `collect`, and can also be called alone to repair a stored
group without re-crawling the period.

Collection skips ids already stored (`known_ids`), so a second `collect` over
the same selection does not duplicate raw records.

## `normalize.py::OpenAlexNormalizer`

Turns `openalex_works` (raw, MongoDB) into `Publication` and `Person` rows.
Beyond the plain mapping it does the following:

- **Funding** (`_funding`): `awards` give `funder_display_name` and
  `funder_award_id`, stored as `Funding.funder` and `Funding.grant_id`.
  Organizations from `funders` that no award covers (matched by
  `awards.funder_id = funders.id`) are kept with `grant_id=None`. An identical
  funder and grant pair is stored once; an entry without a grant number is
  dropped when the same funder has a numbered one. Different grant numbers of
  one funder stay as separate entries. Numbers are stored unchanged and fully
  empty entries are skipped.
- **Publisher markup** (`_clean_markup`): chemistry and physics publishers
  deposit formulas as MathML or HTML tags (`<mml:math>`, `<sub>`) and OpenAlex
  serves the title verbatim. A formula collapses to text without inner spaces
  and stays attached to what it indexes (`monolayer WSe2`, not
  `monolayer WS e 2`); other markup just loses its tags.
- **Non-person author names** (`NOT_A_PERSON_NAME`): author slots sometimes
  hold a venue, a collaboration or a contact address, and OpenAlex mints
  author entities for them. A keyword regex (`association`, `committee`,
  `consortium`, ...), plus e-mail and URL patterns, keeps them from becoming a
  `Person`.
- **Local id for an unidentified author** (`_fallback_person_id`): a fresh
  record can arrive with `author.id = null` but a name and often an ORCID.
  Instead of losing the authorship it gets a deterministic id (the ORCID if
  present, otherwise a hash of the name) that `dedup` can later fold into the
  real author.
- **External author cap** (`EXTERNAL_AUTHORS_LIMIT = 500`): consortium papers
  carry hundreds of authors. All ITMO authors are always kept; external ones
  are capped so one large consortium does not flood the graph.
- **Re-normalization keeps enrichment.** Existing enrichment data,
  `_processing` and ids folded by dedup (`merged_ids`) survive a repeat run:
  new raw data only adds to a row. This works across groups, because entities
  in MongoDB are global: the same work id seen from another, overlapping group
  finds the same state (`OpenAlexNormalizer._seed`, see
  [../storage.md](../storage.md)).

## `enrich.py::Enricher` and `pipeline/stages/base.py`

`Enricher.run(stage_name, selection, force)` runs one stage or all of them
(`ALL_STAGES`, in the order fixed in `pipeline/stages/__init__.py`: `persons`,
`departments`, `code_links`, `link_relevance`, `emails`, `repositories`,
`repo_people`, `dedup`, `github_match`, `author_names`). Atomicity is
per-document in MongoDB (see [../storage.md](../storage.md)).

The order is not arbitrary. `emails` reads the full text downloaded by
`code_links` and runs before `github_match`, so that a found address can
identify an account. `github_match` needs both the accounts collected by
`repo_people` and the authorships already folded by `dedup`.

`repositories` and `repo_people` are two halves of one job, split on purpose:
the first fetches repository metadata, the second the people behind it. Each
has its own `processing` status, so they go stale and are re-run
independently (see [repo-people.md](repo-people.md)).

`OPTIONAL_STAGES` are outside the default run and start only by name. There is
one, `social_graph`: it walks outward from accounts that are already
confirmed, so it only makes sense after `github_match` has confirmed some, and
it costs hundreds of API requests per run.

`EnrichmentStage` is the shared base class:

- `needs_attempt(state)` is `True` under `--force`, if `state is None`, or if
  the status is `NOT_STARTED` or `FAILED`; everything else is skipped.
- `selected(entity, id)` filters by `PreparedSelection` when the run is
  limited with `--input`.
- `in_scope(entity, id)` does the same, except that a selection on a
  *different* entity does not filter. A stage that reaches its rows through
  several entities decides for itself what a publication-scoped run means for
  each of them.

Each stage has its own note:
[persons.md](persons.md), [departments.md](departments.md),
[code-links.md](code-links.md), [emails.md](emails.md),
[repositories.md](repositories.md), [repo-people.md](repo-people.md),
[dedup.md](dedup.md), [github-match.md](github-match.md),
[author-names.md](author-names.md), [social-graph.md](social-graph.md). The
`author_names` note also covers its separate LLM contract, retries and the
repair of invalid `completed` rows.

## Resumability

`_processing` on each row survives between `pauk enrich` runs, and a repeat run
touches only `NOT_STARTED` and `FAILED` rows. Stages save in one of two ways:
`write_models()` once at the end of `run()` over all rows, or `upsert_models()`
per row right after an external response, so an interrupted run does not
repeat finished requests.
