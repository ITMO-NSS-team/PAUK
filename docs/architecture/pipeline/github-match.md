# `github_match` stage

**What this covers:** how a collected GitHub account is linked to an ITMO
author.

**Files involved:** `pauk/pipeline/stages/github_match.py`.

Reads the `github_profiles` collection (accounts collected by `repositories`,
`repo_people` and `social_graph`), `persons` and `repositories`. Writes
`Person.github`, `Person.email` and `Person.contributed_to`, plus a decision
journal `github_matches.jsonl` in `data/audit/<group>/`.

## What gets compared

Every account against every author would be millions of pairs, nearly all with
no signal at all. Only three groups are considered, each an existing
relationship:

- **bridge**: the account was found on a repository cited by a publication that
  has ITMO authors;
- **email**: the account's address equals the author's address;
- **name**: the full name (not a single word) equals the author's name.

A single-word name does not enter the index: half the sample shares a surname,
and such a match means nothing.

## Signals and weights

`score_account` returns the list of signals that fired and their total weight:

| signal | weight | source |
|---|---|---|
| `email_exact` | 1.0 | account and author share an address |
| `name_exact` | 0.6 | same set of name words |
| `name_alternative` | 0.6 | the same, but only through an OpenAlex spelling the main name does not back |
| `name_fuzzy` | 0.4 | similar name, threshold `NAME_FUZZY = 0.86` |
| `itmo_email` | 0.3 | account address on `@itmo.ru` |
| `login_surname` | 0.3 | author's surname inside the login |
| `owner` | 0.3 | the repository belongs to the account |
| `org_itmo` | 0.3 | the repository is under an ITMO organization |
| `itmo_profile` | 0.2 | ITMO or Saint Petersburg in the profile |

`itmo_profile` uses the regex `ITMO_IN_TEXT` over `company`, `location` and the
description: `itmo` as a separate word (the word boundary matters, otherwise
the Norwegian RITMO becomes ours), and the city in every spelling labs use:
`Saint Petersburg`, `St. Petersburg`, `St-Petersburg`, `Sankt Petersburg`, and
the Cyrillic `Санкт-Петербург` (St. Petersburg) with a hyphen or a space.

The patterns are separate: `ITMO_IDENTITY_PATTERN` is an explicit mention of
ITMO, `PETERSBURG_PATTERN` a weak geographic signal. `github_match` combines
them for `itmo_profile`. `social_graph` treats only the first as confirming an
organization, because a city does not establish membership.

`org_itmo` means the repository belongs to an organization with ITMO in its
login itself (`ITMO-NCCR`, `ITMO-PTDC-Team`): only `ITMO_IDENTITY_PATTERN` is
used, without the geographic signal. `social_graph` has a broader rule that
also counts the catalog and an explicit ITMO mention in the organization's
profile. A confirmed ITMO contributor only yields the diagnostic status
`possible`, since an ITMO employee may commit to someone else's lab.

Word order in a name means nothing: `Petrov Ivan` and `Ivan Petrov` are the same
name. All known spellings on both sides are compared: how the author published
(`name_variants`, ORCID `other_names`) against what the account is signed with
(`name`, `login`, names from commits).

OpenAlex lumps namesakes into one author, so `name_variants` also holds other
people's names. A variant counts as `name_exact` only when all its words are in
the main name (`Daniil Razdyakonov` for `Daniil S. Razdyakonov`) or there is no
main name. Otherwise (`Pavel Gulyaev` for `P. V. Gulyaev`, `Xin Zhang` for
`Xi Zhang`) it gives `name_alternative`. ORCID names are the author's own and
always count.

## Three outcomes

`decide` does not sum weights; it looks at the composition:

- an address match decides by itself: `matched`, unconditionally;
- an exact name is `matched` if there is a bridge or at least one corroborating
  signal (`CORROBORATING`), otherwise `review`;
- `name_alternative` is `matched` on the same terms, otherwise `rejected`: a
  spelling that may belong to a namesake is not worth a question on its own;
- a similar name is `matched` only with a bridge together with corroboration,
  and `rejected` without a bridge;
- everything else is `rejected`. Corroborating signals identify nobody on their
  own: "works at ITMO and owns a repository" fits thousands of people.

A `review` pair is not written to `Person`; it stays in the journal with its
full set of signals, which is what a person looks at.

When several authors claim an account with equal weight, a shared address wins,
then a bridge from the author's own paper. Only a tie on all three downgrades
the match to `review` (`evidence.ambiguous`, reason "several authors fit this
account equally well"): picking either would be a guess. This is usually one
person split by OpenAlex into two nodes that `dedup` has not yet folded, which
is why `dedup` should run before `github_match`. Authors are compared in id
order, so a tie names the same author on every run.

## Questions

An account the rules cannot place becomes one question in the review queue. It
names every author still in the running, best first (`candidates` on the row),
and is answered by choosing the author the account belongs to, or "none of
them". An account with a single author is answered "theirs" or "not theirs".

People's answers come before the choice. An author somebody confirmed or chose
wins it, even where the signals alone would reject the pair, unless another
author the rules would link fits as well or better. Such a "yes" was given
without that author in view (a namesake at another university, a record the
account shares an address with), so the account is asked about again, naming
all of them (reason "answered before, but the question did not name every
author that fits"). Choosing one author is a "no" to the others in the question.
Authors somebody refused, one by one or all at once, are left out of the choice,
so the next question names only authors nobody has refused. If the rules alone
would now link a refused author, the answer goes to the disputes instead.
Answers given when an account was asked about one author at a time keep
applying; where two answers cover one pair, the later one counts.

A run also closes the open questions it has settled itself. Only for an account
whose decision it wrote: a link a scoped run does not write leaves the account's
questions alone. And only questions whose authors it covers (a scoped run covers
its selection only), directly or through the record a person was folded into.
Such a question is settled when the account was linked, when nobody fits it any
more, or when the question asked now names other authors, for instance after a
namesake appeared. A closed question gets `auto_resolved_at` and no verdict, so
it leaves the queue and comes back if a later run holds it again. Answered
questions are never touched.

## `confidence`

`high` when the match rests on something that belongs to this person alone:
their address, a surname in the login, ownership of the repository (`STRONG`),
or a bridge from their own paper. Otherwise `probable`: the name matched plus a
signal shared by the whole organization. This is not doubt about the decision
(doubtful ones go to `review`), only a note for whoever looks at the graph
later.

## What goes into `Person`

The login, the address (if the author had none, the one the account's commits
are signed with) and `contributed_to` with the role `owner` or `contributor`,
which is the `CONTRIBUTED_TO` edge. The edge is created only to a repository
present in the `repositories` collection; repositories walked by `social_graph`
are not stored there, so they cannot be referenced.
