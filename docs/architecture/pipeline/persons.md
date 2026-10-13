# `persons` stage

**What this covers:** how a person is enriched from the author's own records,
and the separate Crossref pass over publications in the same stage.

**Files involved:** `pauk/pipeline/stages/persons.py`.

Enriches `Person` from the author's own records: the OpenAlex author API and
ORCID. The same stage also contains a separate Crossref pass, unrelated in
code: a backfill of coauthors' ORCIDs by the publication's DOI.

## Main pass: one author at a time

For each `Person` that needs attention (`needs_attempt`):

1. With an `openalex_id`, `GET /authors/{id}`: updates `name_raw`, collects name
   variants (`name_variants`), the ORCID (if not yet known) and the author's
   affiliations from their own record (`affiliations` plus
   `last_known_institutions`; the latter has no years and is a fallback for a
   year with no explicit entry).
2. With an ORCID, `GET /record`: affiliations from ORCID employments
   (`start`/`end` year; an open employment covers everything from its start to
   the current year), plus what the author wrote about themselves:
   - **all** listed addresses (`Person.emails`), not just the first: a third of
     those who list an address list several, and an account is recognized by
     the one its commits are signed with. `Person.email` gets the first one if
     there is none yet;
   - name spellings (`other_names`): the preferred `credit-name` and the
     registered variants; a commit signed `Dangana, RS` is recognized only
     through them;
   - profile links (`researcher-urls`): GitHub, Google Scholar, GitLab,
     LinkedIn, the first of each kind. A GitHub link given by the author is the
     most reliable tie there is: there is nothing to match. Other links, except
     known directories (ResearchGate, Scopus, Web of Science and similar),
     become `homepage`, from which the `emails` stage later extracts an
     address.
3. `is_itmo` is recomputed: at least one affiliation with ITMO's ROR anywhere
   makes the person ITMO, even if no single work claimed it (`ITMO_ROR_ID` is
   the same as in `normalize.py`).
4. **Filling a missing authorship affiliation**
   (`_fill_missing_affiliations`): self-deposit venues (Zenodo, SSRN) often do
   not say where a given coauthor worked at the time. The author's own records
   know this, so the affiliation covering the publication year is filled in
   (`_affiliation_for_year`: an exact year match first, then the most recent
   dated one, then an undated one as a last fallback). `affiliation_source`
   records that the value was *filled in*, not declared by the work itself, in
   case better data with an explicit affiliation arrives later and must not
   silently lose to the filled value.

`Person.github` is filled from the link the author gave in ORCID and by the
`github_match` stage, which matches collected accounts to authors by e-mail,
name and citation ([github-match.md](github-match.md)). Neither overwrites an
already known login.

`Person.google_scholar` is filled from the ORCID link.

Resumability state is kept per source: `openalex_author` and `orcid` on
`Person`, `crossref` on `Publication`. After each response the changed row is
saved to Mongo immediately, so an interruption does not force finished
responses to be fetched again. The state has a `request_key` (OpenAlex id, ORCID
or DOI): a completed request for an old identifier does not block a request
once the identifier has been refined.

## Crossref pass: per publication, not per author

A separate loop whose resumability key is not `Person.processing["persons"]`
but `Publication.processing["crossref"]`. For each publication with a DOI whose
Crossref step is not complete: `GET /works/{doi}`; each author in the Crossref
response is matched by surname (`casefold` of the last word of `name_raw`)
against the local `Person` rows of that publication. On an unambiguous match
(exactly one local author with that surname) who has no ORCID, the ORCID from
Crossref is set.

A publication without a DOI is marked `NOT_APPLICABLE` at once: there is
nothing to look up in Crossref without a DOI.

Matching is by surname alone, so dedup (`pipeline/stages/dedup.py`) treats
`Person.orcid` as not fully authoritative: if a raw OpenAlex record exists for
the author, dedup takes the ORCID from there, overriding (including resetting
to `None`) whatever this Crossref backfill may have set.
