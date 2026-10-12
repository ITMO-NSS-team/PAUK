# `pauk/models/`: prepared-layer schema

**What it covers:** which entities and fields exist in the prepared layer
and what each field means.

**Files:** `pauk/models/publication.py`, `person.py`, `department.py`,
`organization.py`, `repository.py`, `relations.py`, `processing.py`,
`__init__.py`.

Pydantic models of the rows stored in the prepared MongoDB collections (see
[storage.md](storage.md)). Nothing here touches the network or holds business
logic: only the shape of the data, plus a few small computed properties on
the relation-like models.

## `publication.py`

- **`Publication`**: id is the OpenAlex work ID without the URL prefix.
  - `type` is the OpenAlex work type (`article`, `preprint`, `software`,
    `dataset`, ...); `code_links.py` uses it, for example, to detect
    archived Zenodo deposits ([pipeline/code-links.md](pipeline/code-links.md)).
  - `fields` are top-level OpenAlex topics; person dedup uses them as a weak
    same-research-area signal.
  - `full_text` is text extracted from the PDF. It is `None` if no PDF was
    found or it could not be parsed; do not confuse it with `abstract`, which
    comes straight from OpenAlex.
  - `mentions_links: list[MentionsLink]` is a parallel representation of code
    links. The path that actually loads `MENTIONS_LINK` relationships into the
    graph goes through `repo_links` (`RepoLink`/`CodeLink`); see
    [neo4j-graph.md](neo4j-graph.md).
  - `versions` and `merged_ids` are the merge ledger (below).
  - `pdf_urls` are unique candidates from `best_oa_location` and `locations`,
    in load order; they are combined on re-normalization and dedup.
- **`PublicationVersion`**: one OpenAlex record folded into this publication
  by dedup: a preprint, a record version, or a duplicate from OpenAlex
  reindexing. It keeps the title, DOI, journal, date, abstract and authors of
  **that specific record**, so merging loses no source; everything stays
  available through `versions` even if the surviving publication took other
  field values.
- **`VersionAuthor`**: an author of one version as OpenAlex listed them
  (`person_id`, `name`, `position`). It is not the publication's current
  author list, which comes from `Authorship` relations to `Person`.
- **`Funding`**: a grant: `funder`, `grant_id`.

## `person.py`

- **`Affiliation`**: a person's workplace as one source states it (`name`,
  `ror`, `years`, `source`: `"openalex"` or `"orcid"`). Self-deposit
  platforms (Zenodo, SSRN) often omit a coauthor's affiliation on a given
  work; `PersonsStage` then picks the affiliation for that year from the
  author's own OpenAlex/ORCID records instead of what the work says.
- **`Person`**: id is the bare OpenAlex author ID (one person, one node; there
  is no `itmo_*`/`external_*` split).
  - `is_itmo` is a boolean, not a label. At least one ITMO affiliation
    anywhere makes the person ITMO for good: the property is "sticky" (see
    `graph/client.py::upsert_person_nodes_batch`).
  - Russian name parts (`first_name_ru`, `second_name_ru`, `surname_ru`) and
    `name_ru`/`name_en` are filled by the `author_names` stage.
  - `email` is the one address shown on the card; `emails` are all known
    addresses, which `github_match` uses to recognize an account (an account
    is identified by the address it commits with, not the one chosen for
    display).
  - `github` is filled from ORCID links and by the `github_match` stage;
    `google_scholar` comes from ORCID links.
- The block under the `# Stubs` comment (`scopus_id`, `researcher_id`,
  `h_index`, `wikipedia` and others) holds fields no pipeline stage fills.
  The exceptions are `other_names`, `homepage`, `linkedin`, `gitlab_username`
  and `name_ru`/`name_en`, filled by `persons` from ORCID
  ([pipeline/persons.md](pipeline/persons.md)) and by `author_names`. The
  stubs are kept on purpose so the model, connector and visualization
  already know the data shape when a real source appears.

## `department.py`

- **`Department`**: `id`, `name_en`, `name_ru`, `name_variants` (spellings
  that `departments.py` uses to match a department in affiliation text),
  `context_aliases` (generic names that match only inside an affiliation
  segment carrying an ITMO marker), `parent_id` or `organization_id` (at most
  one is set: a sub-unit points to its parent department, a top-level unit to
  its organization), and `kind` (`megafaculty`, `school`, `faculty`,
  `institute`, `center`, `department`, `lab`, `unit`; used for styling). The
  source is `data/static/departments_catalog.json`
  (`pauk/storage/static.py::StaticStore`); the id is the human-readable `uid`
  of the catalog entry.

## `organization.py`

- **`Organization`**: a top-level organization (university, institute,
  company) at the root of the hierarchy: `id`, `name_en`, `name_ru`,
  `ror_id`, `country`, `type`. Departments attach to it through
  `Department-[:PART_OF]->Organization`.

## `repository.py`

- **`Repository`**: id is `github_{owner}_{name}` in lower case after
  canonicalization (see [pipeline/dedup.md](pipeline/dedup.md)).
  - `github_id` is the numeric GitHub id; it survives renames and ownership
    transfers and is the dedup key.
  - `cited_urls` are all URLs under which the repository was ever cited
    (before canonicalization); the graph loader uses them to resolve old
    links to this node.
  - `publication_ids` are only the publications whose authors produced the
    repository; plain mentions stay in `RepoLink`.
  - `merged_ids` are ids of repositories folded into this one.
- **`GitHubProfile`**: an account found behind a repository, as its owner or
  commit author (`login` is unique, separate from `id`). Besides the profile's
  own fields (`name`, `company`, `location`, `description`) it carries what
  authors are matched against: `emails` and `commit_names` (from the git
  identity in commits, because profile pages usually hide the address) and
  `repos` (repositories where the account appeared). `profile_fetched` marks
  whether `GET /users/{login}` has answered, as opposed to a stub built from a
  nested owner object. Only `company` goes into the graph; the rest is
  matcher evidence and real people's addresses (see
  [neo4j-graph.md](neo4j-graph.md)).
- **`LinkCandidate`**: a code link found in a paper but not yet matched to a
  known `Repository` (`id` is the URL itself).
- **`LinkOccurrence`**: one occurrence of a link: `context` (surrounding
  text) and `page_number` (`None` means the abstract; PDF pages start at 1),
  plus `raw_url`, `raw_fragments`, `candidate_urls` (alternative readings of
  a URL broken across lines) and `continuous`. A link seen in the abstract
  and on several pages has several occurrences.
- **`CodeLink`**: `url`, `host`, `occurrences: list[LinkOccurrence]`,
  `classification_status` (`pending`, `classified`, `failed`),
  `is_relevant`/`llm_confidence`/`llm_reason` (filled by `link_relevance.py`,
  except for the deterministic Zenodo archive), and `availability`
  (`unchecked`, `available`, `not_found`, `failed`). The `url_ambiguous`
  property is true when every occurrence has more than one candidate URL. See
  [pipeline/code-links.md](pipeline/code-links.md).
- **`RepoLink`**: a wrapper `{publication_id, links: list[CodeLink]}`, one
  document per publication in the `repo_links` collection.

## `relations.py`

Relation models embedded in the parent row (not separate collections):

- **`Authorship`**: `publication_id`, `position`, `affiliation`,
  `affiliation_source` (`None` if the paper itself gave the affiliation;
  `"openalex"` or `"orcid"` if `PersonsStage` filled it in because the
  source record had none), `is_corresponding`.
- **`Contribution`**: `repository_id`, `role`.
- **`MentionsLink`**: `target_kind: "repository" | "candidate"` plus
  `context`, `page_number`, `is_relevant` and so on, the same discriminated
  shape as `CodeLink`. `graph/extract.py` has a recipe for this field
  (`NODE_REGISTRY["publication"]`), but the path that actually loads links
  goes through `repo_links` and `graph/jsonl_loader.py::extract_repo_links()`;
  see [neo4j-graph.md](neo4j-graph.md).

## `processing.py`

- **`ProcessingStatus`**: a `StrEnum`: `not_started`, `completed`,
  `completed_empty`, `not_applicable`, `failed`. It is shared by all stages,
  and its only real job is to control retries in
  `EnrichmentStage.needs_attempt()` (`NOT_STARTED`/`FAILED` retry, anything
  else is skipped without `--force`). Do not read extra meaning into it: the
  reason for a failure goes into `ProcessingState.error` as text, not into a
  separate status.
- **`ClassificationStatus`**: `pending`, `classified`, `failed`, used by
  `CodeLink` and `MentionsLink`.
- **`ProcessingState`**: `status`, `request_key`, `attempts`, `finished_at`,
  `error`, `result_count`. `request_key` ties the result to a specific
  external API input (OpenAlex id, ORCID, DOI).

## `__init__.py`

Re-exports the main models (everything above except the processing types, apart from `ClassificationStatus`) as one flat list. Other code always writes
`from pauk.models import X` and does not reach into the internal modules.
