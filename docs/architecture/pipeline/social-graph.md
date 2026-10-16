# `social_graph` stage

**What this covers:** how employees are found whose code no paper cited.

**Files involved:** `pauk/pipeline/stages/social_graph.py`.

Not part of the default run: it is in `OPTIONAL_STAGES` and starts only by name
(`pauk enrich --group <g> social_graph`). It makes sense only after
`github_match` has confirmed someone, and a run costs hundreds of API requests.

## Why

`repositories` reaches only the people who worked on code a paper cited. Most
employees who write code were not cited: the repository sits on their own
account or on a lab's account, and nothing in the publications leads to it.

## How the walk goes

The starting point is a confirmed account or an ITMO organization. The stage
takes that account's public repositories and collects participants from them;
those participants become candidates like any others.

`github_match` runs between rings: it turns a candidate of this ring into a
starting point for the next. Without it the walk would end after the first
ring. A ring that yields no new repository ends the walk; `MAX_RINGS = 5` is a
ceiling in case it never converges.

The walk starts only from confirmed accounts. Starting from all candidates
would give several hundred starting points and thousands of repositories.

## Which organization counts as ours

An organization is confirmed if its login is in the curated catalog
(`itmo_github_orgs.json` in the static directory) or if it explicitly names
ITMO in its login, name, description or company. Otherwise every organization
whose library a paper cited would qualify, google and microsoft included.

Saint Petersburg is only a weak signal: such an organization is logged as
possible but not walked. A link to a confirmed ITMO contributor also does not
confirm an organization, since an employee may commit to someone else's lab. A
real lab with an empty profile is added to the curated catalog. If the owner of
a known repository is in the catalog, a missing profile or an unknown account
type does not prevent starting the walk from it. An explicit type `user` rules
out using the account as an organization seed even if it is in the catalog. For
owners outside the catalog a profile with type `organization` is required.

## Limits of the walk

`MAX_REPOS_PER_SEED = 30`, sorted by last modification time: a prolific account
has hundreds of repositories, and people are on the ones it touched recently.
Forks are skipped, since they carry the upstream project's participants, not
this account's.

Visited repositories are remembered from two sources: the `repositories`
collection and the `repos` field on collected profiles. Reading only the first
would make every later run walk the same hundreds of repositories again, since
the walk itself writes nothing to `repositories`.
