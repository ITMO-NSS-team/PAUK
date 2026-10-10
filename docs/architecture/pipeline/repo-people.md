# `repo_people` stage

**What this covers:** how the people behind a repository are collected, and why
this is a separate stage rather than part of `repositories`.

**Files involved:** `pauk/pipeline/stages/repo_people.py`.

The owner and everyone credited with commits are candidates against whom
authors are later matched ([github-match.md](github-match.md)). The stage reads
`contributors` and `COMMIT_PAGES` pages of commits, extracts git identities
(`emails`, `commit_names`) from them, and filters out bots and organizations.

## Why a separate stage

Both jobs used to share one `processing` status, so refreshing a repository's
metadata meant walking all contributors again and re-requesting every profile.
In a measured run that was 6143 requests out of 6864, and the hourly GitHub
quota was spent on fields that arrive free in the repository response body.

The `--skip-accounts` flag would not fix this: the status would stay single,
the row would claim `repositories: completed` while holding people data from
the previous run, and neither a person nor the code could tell the fresh half
from the stale one. Two stages give two statuses, and each goes stale on its
own.

A measurement on `aimclub/FEDOT` (40 contributors):

| run | requests |
|---|---:|
| `enrich repositories --force` | 2 |
| `enrich repo_people --force` | 44 |
| `enrich repo_people` (row already `completed`) | 0 |

## The owner's type comes from the profile

Whether the owner is a person or an organization is read from the already
stored `GitHubProfile.type`, which `repositories` set. The type is lowercase
there, so `_is_person` compares case-insensitively; for contributors the same
attribute arrives from the API as `User`.

## A filled profile is not requested again

`GET /users/{login}` is made only for a login that has not yet had this request
answered (marked by `profile_fetched` on the profile), or under `--force`.
Judging by `html_url` was not possible: the `repositories` stage writes that
field into the owner's profile stub itself, so the gate would close on data the
pipeline had just made up and the repository owner would never be requested.
The content of the fields does not replace `profile_fetched`: name and e-mail
may come from commits after a failed profile request. Old profiles without the
marker are requested the next time their repository is processed. An account
seen on a second repository learns nothing new from this request, so the marker
avoids those calls once a profile load is confirmed.

## An error is `FAILED` for this stage only

GitHub answers `403` for repositories it has not yet analyzed. A failed people
collection is recorded as `processing["repo_people"] = FAILED` with the reason;
the metadata stage's status is unaffected, since another stage collects the
metadata.

This also applies to `GET /users/{login}`: an exception is not replaced by an
empty response but reaches the repository handler. Profiles already received
are saved; the next regular run retries the failed processing and skips
profiles with `profile_fetched=true`. A failed refresh does not overwrite the
old profile. Rows wrongly marked `completed` earlier are not reset
automatically; fixing them needs a separate re-run.

## What a publication-scoped run means here

`in_scope` alone does nothing for repositories: a selection aimed at
publications is a selection on a *different* entity, and it lets everything
through (see [overview.md](overview.md)). The stage decides for itself:
`_repo_in_scope` with `--entity publications` (and `repo_links`) keeps only rows
whose `publication_ids` intersect the selected ones. Without it,
`enrich repo_people --input pubs.txt --entity publications` would walk every
pending repository of the group and spend GitHub quota on ones nobody asked
about. `RepositoriesStage` takes its scope from the `RepoLink` row it works
on; here the `Repository` already exists and carries its publications itself.

## Writing

`upsert_models`, not `write_models`: the stage changes only `contributors` and
its own `processing` in the repository row, and must not redefine the group's
membership (see [../storage.md](../storage.md)).
