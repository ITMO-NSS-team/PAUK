# `repositories` stage

**What this covers:** how a found code link becomes an enriched `Repository`
through the GitHub API.

**Files involved:** `pauk/pipeline/stages/repositories.py`.

Reads the `repo_links` collection (every link found by `code_links`). For each
link of the form `github.com/owner/repo` (exactly two path segments) it queries
the GitHub API.

## One request per repository per run

Many publications can mention the same repository. All `cited_urls` are
collected across mentions, and GitHub metadata is fetched for every found link
regardless of relevance. But `Repository.publication_ids` holds only
publications whose `CodeLink.is_relevant=true` and whose URL is not an
unresolved line-wrap alternative; the graph builds `IMPLEMENTS` from that
field. A third-party mention stays in `repo_links` and later becomes only
`MENTIONS_LINK`.

Before the pass, the stage removes the publications being processed from
`publication_ids` and adds back only the confirmed pairs. So a reclassification
`true -> false/null` drops the old authorship link without touching
publications from other groups. The GitHub request itself happens at most once
per repository per run (`attempted_repo_ids`); otherwise `--force` on a large
group would repeat it once per citing publication.

Line-wrap alternatives go through the same metadata request. The result is
written to `CodeLink.availability` (`available`, `not_found`, `failed`) and the
failure reason to `availability_error`. Only a 404 on the repository request
itself means `not_found`; 403, 502 and network errors mean `failed`. A later
README failure does not undo the fact that the repository is available. These
statuses do not pick the correct spelling of a link automatically; the original
alternatives stay available for review.

## What a successful request fills in

`GET /repos/{owner}/{name}` gives `html_url` (the canonical URL, which can
differ from the cited one after a rename), `name`, `description`, `stars_num`
and the owner (`owner_login`, which creates or updates a `GitHubProfile`).
`has_readme(owner, name)` is a second call, `GET .../readme`: `404` gives
`False`, anything else `True`. The main payload does not report a README at
all.

`topics`, `language`, `forks_count`, `archived`, `fork` and `license.spdx_id`
come from the same response body at no extra cost. `last_updated` is
`pushed_at`, not `updated_at`: the latter also moves on a star or a description
edit and says nothing about whether the code is alive.

`repo.github_id` is GitHub's numeric id, stored apart from the URL. It survives
renames and ownership transfers, and repository dedup is built on it
([dedup.md](dedup.md)).

## The people behind a repository are not collected here

The owner and the committers are collected by the separate `repo_people` stage,
which has its own `processing` status; the reasoning is in
[repo-people.md](repo-people.md). This stage keeps only the owner's profile
stub: the nested `owner` object in the GitHub response carries a login, a type
and a URL, and these fill in the existing `GitHubProfile` instead of replacing
it, which would erase addresses and names the same person left on other
repositories. For an owner of type `organization` it additionally makes one
`GET /users/{login}` call per organization per run, filling name, description,
location and company, which `social_graph` reads to recognize an ITMO lab.

## Two sources of work

The work list is the union of two sets:

1. **`repo_links` rows**: this is how repositories are *found*. The id
   `github_{owner}_{name}` is derived from the link, and a row is created if it
   does not exist;
2. **existing `repositories` rows of the group**: this is how they are
   *enriched* further.

The second pass exists because rows imported straight into the collection with
no link in `repo_links` (for example curated groups) would otherwise never be
reached.

The second pass is keyed by the id derived from the **row's own URL**, not by
`repo.id`: after re-keying to the canonical identity the two differ, and
without a shared key `--force` would take such a row twice, once by the cited
link and once in the second pass.

The second-pass set is taken **before the first request** (`_url_repo_id`
computes the keys once, at the start of `run()`). A successful request rewrites
`repo.url` to the canonical URL GitHub redirects to; recomputing keys after the
first pass would give the same row a new key, miss it in `attempted_repo_ids`
and fetch it again. For the same reason the first pass claims not only the key
of the cited link but also the key of the row's own URL, before the request.

One key can hold **several rows**: a curated import brings its own id, while
the link pass derives an id from the cited URL, and both point to the same
owner/name. It is one repository, so the rows are folded rather than
overwritten by key (`_fold_duplicates`). The row that already has a `github_id`
wins (it was seen by the API, so its url and name are canonical); on a tie the
smaller id wins, so the winner does not depend on read order. Losers hand over
`publication_ids` and `cited_urls` and leave their id in the winner's
`merged_ids`, which the graph loader uses to resolve links to the old node.

## An error means `FAILED`, not a lost link

A failed request (404, rate limit, network error) sets
`Repository.processing["repositories"] = FAILED` with the reason. The stub
`Repository` (only url and name) is still stored, but the graph loader does not
load it until the stage succeeds (`jsonl_loader.py::_stage_failed`). The link
stays visible in the graph as a `LinkCandidate` until a retry succeeds.

## Re-keying to the canonical identity

After the main loop, a separate pass (`_canonical_repo_id`) moves rows that were
created under the cited URL to `github_{owner}_{name}` built from the payload
**returned by the API** (not from the original URL). A repository renamed on
GitHub, or cited in a different letter case, must not split into two graph
nodes that share one `Repository.url` constraint.

Re-keying is also a merge, so the id a row was stored under goes into
`merged_ids` rather than being overwritten: already published edges may point
to it, and the graph loader resolves them through that list. The same holds for
a losing row: its own id and everything it had already absorbed move into the
winner's `merged_ids`. The canonical id itself is excluded, so a row is never
listed as merged into itself.
