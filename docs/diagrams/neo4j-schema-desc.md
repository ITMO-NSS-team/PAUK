# Neo4j graph schema description

A per-field description of nodes and relationships: the **actual schema as the
code builds it** (`pauk/graph/extract.py::NODE_REGISTRY` plus the unique keys
from `pauk/graph/schema.py::CONSTRAINTS`). A diagram of the same schema is in
[`neo4j-schema.md`](neo4j-schema.md); the connector context is in
[`../architecture/neo4j-graph.md`](../architecture/neo4j-graph.md).

Only fields with a real uniqueness constraint are marked (*Unique*). Fields
missing from the `prop_fields` of the corresponding `NodeSpec` do not reach the
graph even if the pydantic model has them (see
[`../architecture/models.md`](../architecture/models.md)).

## 1. Nodes

### 1. `Person`

> **Node label:** `Person`
> A researcher or author, from ITMO or external, told apart by the `is_itmo`
> property, not by the label.

* **`id`** (`ID`, *Unique*): author identifier (the bare OpenAlex author ID).
* **`is_itmo`** (`Boolean`): ITMO membership. A "sticky" property: once set to
  `true`, a later external row for the same person never lowers it to `false`.
* **`openalex_id`** (`String`): OpenAlex identifier.
* **`orcid`** (`String`): ORCID identifier.
* **`name_raw`** (`String`): the name as OpenAlex gave it.
* **`name_en`** (`String`): full name in English.
* **`first_name_en`** (`String`): first name in English.
* **`second_name_en`** (`String`): second name (patronymic) in English, if known.
* **`surname_en`** (`String`): surname in English.
* **`name_variants`** (`List[String]`): name spelling variants.
* **`name_ru`** (`String`): full name in Russian.
* **`first_name_ru`** (`String`): first name in Russian.
* **`second_name_ru`** (`String`): second name (patronymic) in Russian.
* **`surname_ru`** (`String`): surname in Russian.
* **`email`** (`String`): one e-mail address, for the card.
* **`emails`** (`List[String]`): every known address; the matcher identifies
  accounts by them.
* **`degree`** (`String`): academic degree.
* **`github`** (`String`): GitHub profile.
* **`google_scholar`** (`String`): Google Scholar profile.
* **`thesis`** (`String`): dissertation or qualifying work (ITMO persons only).
* **`scopus_id`** (`String`): Scopus identifier.
* **`researcher_id`** (`String`): Web of Science ResearcherID.
* **`dblp_id`** (`String`): dblp identifier.
* **`other_names`** (`List[String]`): other names and pseudonyms.
* **`biography`** (`String`): biography.
* **`country`** (`String`): country.
* **`homepage`** (`String`): personal website.
* **`gitlab_username`** (`String`): GitLab username.
* **`linkedin`** (`String`): LinkedIn profile.
* **`twitter`** (`String`): Twitter/X profile.
* **`wikipedia`** (`String`): Wikipedia page.
* **`works_count`** (`Integer`): number of works.
* **`cited_by_count`** (`Integer`): total citations.
* **`h_index`** (`Integer`): h-index.
* **`i10_index`** (`Integer`): i10-index.
* **`counts_by_year`** (`JSON`): per-year statistics.
* **`status`** (`String`): employee or researcher status.
* **`created_at`** (`Timestamp`): record creation time.
* **`enriched_at`** (`Timestamp`): time of the last enrichment.
* **`affiliations`** (`JSON`): raw affiliations.
* **`merged_ids`** (`List[String]`): ids folded into this node by dedup.

---

### 2. `Publication`

> **Node label:** `Publication`
> A scientific publication.

* **`id`** (`ID`, *Unique*): publication identifier (the bare OpenAlex work ID).
* **`title`** (`String`): title.
* **`type`** (`String`): work type.
* **`fields`** (`List[String]`): fields of knowledge.
* **`journal`** (`String`): publication venue.
* **`doi`** (`String`): DOI.
* **`publication_date`** (`Date`): publication date.
* **`year`** (`Integer`): publication year.
* **`has_code`** (`Boolean`): whether a code link was found.
* **`code_url`** (`String`): JSON list of links to the authors' code.
* **`funding`** (`JSON`): funding information.
* **`openalex_url`** (`String`): link to the publication in OpenAlex.
* **`pdf_urls`** (`List[String]`): ordered unique PDF links.
* **`abstract`** (`String`): abstract.
* **`full_text`** (`String`): full text (if extracted from a PDF).
* **`versions`** (`JSON`): publication versions.
* **`merged_ids`** (`List[String]`): ids folded in by dedup.

---

### 3. `Department`

> **Node label:** `Department`
> An ITMO unit (megafaculty, faculty, institute, center, department, lab).

* **`id`** (`ID`, *Unique*): a human-readable uid slug derived from `name_en`.
* **`name_en`** (`String`): name in English.
* **`name_ru`** (`String`): name in Russian.
* **`name_variants`** (`List[String]`): spelling variants.
* **`context_aliases`** (`List[String]`): generic aliases matched only next to
  an ITMO marker in an affiliation.
* **`kind`** (`String`): level: `megafaculty | faculty | institute | center | department | lab`.
* **`parent_id`** (`String`): uid of the parent `Department` (for a nested unit).
* **`organization_id`** (`String`): uid of the `Organization` (for a top-level unit).

Exactly one of `parent_id` / `organization_id` is set; it becomes the `PART_OF`
edge up the hierarchy.

---

### 4. `Organization`

> **Node label:** `Organization`
> An organization, a root of the org hierarchy (ITMO, and co-affiliations).
> Several organizations coexist in the graph as separate roots.

* **`id`** (`ID`, *Unique*): uid slug.
* **`name_en`** (`String`, *Unique*): name in English.
* **`name_ru`** (`String`): name in Russian.
* **`ror_id`** (`String`): identifier in the ROR registry (for ITMO, `https://ror.org/04txgxn49`).
* **`country`** (`String`): country.
* **`type`** (`String`): organization type (`university`, ...).

---

### 5. `Repository`

> **Node label:** `Repository`
> A source code repository (GitHub).

* **`id`** (`ID`, *Unique*): identifier (`github_owner_name`).
* **`name`** (`String`): repository name.
* **`url`** (`String`, *Unique*): repository URL.
* **`github_id`** (`Integer`): numeric GitHub id.
* **`cited_urls`** (`List[String]`): URLs by which the repository was cited before canonicalization.
* **`description`** (`String`): description.
* **`access_date`** (`Date`): date of the check or access.
* **`has_readme`** (`Boolean`): whether a README exists.
* **`stars_num`** (`Integer`): number of stars.
* **`last_updated`** (`Date`): date of the last commit (`pushed_at`, not `updated_at`).
* **`license`** (`String`): license (SPDX identifier).
* **`topics`** (`List[String]`): topics set by the owner on GitHub.
* **`language`** (`String`): main language as estimated by GitHub.
* **`forks_num`** (`Integer`): number of forks.
* **`archived`** (`Boolean`): the repository is marked archived.
* **`is_fork`** (`Boolean`): the repository is itself a fork.
* **`contributors`** (`List[String]`): contributors.
* **`merged_ids`** (`List[String]`): ids folded in by dedup.

---

### 6. `GitHubProfile`

> **Node label:** `GitHubProfile`
> The GitHub account that owns a repository (a user or an organization).

* **`id`** (`ID`, *Unique*): account identifier.
* **`login`** (`String`, *Unique*): GitHub login.
* **`name`** (`String`): display name.
* **`html_url`** (`String`): profile link.
* **`description`** (`String`): profile description.
* **`location`** (`String`): location.
* **`company`** (`String`): employer stated in the profile; used as an extra
  signal when matching an account to an author.
* **`type`** (`String`): account type (user / organization).

---

### 7. `LinkCandidate`

> **Node label:** `LinkCandidate`
> A code-link candidate not yet resolved to a `Repository` (created on the fly;
> it becomes a `Repository` once the repository resolves successfully).

* **`id`** (`ID`, *Unique*): the URL itself.
* **`url`** (`String`): link URL.
* **`host`** (`String`): link host.

## 2. Relationships

### 1. `AUTHORED`

> **Connects:** `Person` -> `Publication`. Authorship of a publication.

* **`position`** (`Integer`): the author's position in the list.
* **`affiliation`** (`String`): the affiliation string in this work.
* **`affiliation_source`** (`String`): where the affiliation came from.
* **`is_corresponding`** (`Boolean`): corresponding author.

---

### 2. `BELONGS_TO`

> **Connects:** `Person {is_itmo: true}` -> `Department`. The author's
> membership in a unit (derived by matching affiliations against the catalog).
> No properties.

---

### 3. `CONTRIBUTED_TO`

> **Connects:** `Person {is_itmo: true}` -> `Repository`. Participation in a
> repository's development.

* **`role`** (`String`): role in development.

---

### 4. `PART_OF`

> **Connects:** `Department` -> `Department` or `Department` -> `Organization`.
> The recursive org hierarchy: a unit is part of its parent, either another
> unit (`parent_id`) or the root organization (`organization_id`). No properties.

---

### 5. `PRODUCED_BY`

> **Connects:** `Publication` -> `Department`. The publication was produced by
> the unit (by the departments of its ITMO authors). No properties.

---

### 6. `MENTIONS_LINK`

> **Connects:** `Publication` -> `Repository` or `Publication` -> `LinkCandidate`.
> A mention of a code link in the publication's text or abstract.

* **`context`** (`List[String]`): text fragments around the link.
* **`page_number`** (`List[Integer]`): pages (`0` = abstract; Neo4j does not
  store `null` in an array property, so the sentinel is not `None`).
* **`classification_status`** (`String`): `pending`, `classified` or `failed`.
* **`is_relevant`** (`Boolean`): relevance verdict.
* **`llm_confidence`** (`Float`): LLM confidence.
* **`llm_reason`** (`String`): LLM rationale.
* **`url_ambiguous`** (`Boolean`): the link has only line-wrap alternatives
  (set only for links with a recorded raw fragment).
* **`candidate_urls`** (`List[String]`): the alternative readings of the link.
* **`availability`** (`String`): `available`, `not_found`, `failed` or `unchecked`.

---

### 7. `DEVELOPED_BY`

> **Connects:** `Repository` -> `Department`. The repository is developed by the
> unit (by the departments of its ITMO contributors). No properties.

---

### 8. `IMPLEMENTS`

> **Connects:** `Repository` -> `Publication`. The repository implements the
> publication. No properties.

---

### 9. `OWNED_BY`

> **Connects:** `Repository` -> `GitHubProfile`. The repository's owner. No properties.
