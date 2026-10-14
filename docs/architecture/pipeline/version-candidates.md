# `pauk versions report`: version candidates that dedup did not catch

**What this covers:** why an exact DOI/title match ([dedup.md](dedup.md)) is
not enough, which signals the heuristic search uses, and why it only reports
and never merges.

**Files involved:** `pauk/graph/version_candidates.py`; the CLI wiring is in
`pauk/cli.py`.

## Why this is separate from `dedup`

The `dedup` stage ([dedup.md](dedup.md)) folds publications whose DOI or
normalized title matches. That is enough for an OpenAlex duplicate and for
many "preprint + version of record" pairs, but not for cases where one work
has several DOIs **and** a different title:

- a preprint whose title was edited for the journal (words reordered, the
  subtitle changed);
- a conference paper later extended into a journal article;
- a Russian original and its English translation (Pleiades/MAIK): another
  DOI, another journal, often a title in another language;
- an eLife-style revision under its own DOI (`10.7554/elife.84874.2`) whose
  title differs by an appended note.

Found on the real graph: of ≈460 groups already folded by `dedup`, 81%
carried **two different DOIs**, so the title made the decision, not the DOI.
The same analysis over unfolded publications found ≈400 more likely pairs
(details and method are in the discussion of the `feature/papers-versions`
branch; the numbers are not stored in the repository).

## Why only `held`, never `merged`

`dedup` treats a name as evidence, not proof (see "Merge policy" in
`pipeline/stages/dedup.py`), and exactly the same holds for a title:
`"Cliques and Constructors in 'Hats' Game. I"` and `"... II"` have an almost
perfect title similarity and share their authors, yet they are two different
papers. So `find_version_candidates` **never** writes `merged_ids` and never
touches the graph: every pair goes to the journal with `status: "held"`, like
an unresolved pair in `dedup_candidates.jsonl`. A person confirms it.

## Signals and buckets

Candidate pairs come from blocking (a shared author with ≤90 works, a shared
rare title word carried by ≤45 works, a shared arXiv id), and then
`classify_pair()` classifies each pair. An author with >90 works does not drop
out of blocking: among their works, those sharing ≥2 title words that are
rare for this author (≤45 of their works) are paired. Otherwise the most
prolific authors, who have the most preprint/journal pairs, would get none
at all.

| Bucket | Condition | Meaning |
|---|---|---|
| `MISSED_BY_NORM` | titles equal after aggressive normalization (unicode, punctuation), DOIs differ | `dedup` normalization failed (unicode variants `µ`/`μ`, LaTeX, a line break) |
| `DOI_SIBLING` | one DOI = the other + `.vN`/`.N` · title similarity ≥0.75 on ≥4 words after folding · neither record is of type `dataset`/`other`/… | a version of the record under its own DOI (without a similar title, or with a deposit type, it is `SUPPLEMENT_OR_REVIEW`: Cifra `10.60797` and AIP mint such DOIs for an article's figures and tables, whose title is a caption) |
| `PREPRINT_PUBLISHED` | one record on a preprint server, the other not · title similarity ≥0.80 · a shared author · date gap ≤3 years | preprint ↔ publication |
| `RU_EN_TRANSLATION` | Cyrillic in the journal/title of one record and not the other · similarity ≥0.75 · a shared author or abstract similarity ≥0.4 | original ↔ translation (only when the original has an English title: titles are compared as text, not translated) |
| `STRONG_FUZZY` | similarity ≥0.90 · author overlap (Jaccard ≥0.30 or ≥2 shared) · gap ≤1100 days | a reworded title, strong corroboration |
| `MEDIUM_FUZZY` | similarity ≥0.78 · author overlap ≥0.50 or abstract similarity ≥0.45 · gap ≤4 years | the same, weaker: the noisiest bucket |

Title similarity is `max(word Jaccard, difflib.SequenceMatcher.ratio())` over
the title after NFKD normalization (diacritics removed, Cyrillic kept;
Cyrillic look-alikes of Latin letters inside a Latin word are replaced with
Latin ones) and removal of punctuation and stop words (English and Russian).
The first catches reordered words, the second point edits (a hyphen, an
ending). A Russian title and its English translation share no letters, so
such a pair is not found by the title alone.

Three buckets are not versions but explained look-alike pairs. They are
journalled too, but are never in `MERGE_BUCKETS`:

- `ERRATUM`: `"Correction to: …"` / `"Erratum to: …"` / `"RETRACTED: …"` /
  `"Retraction notice to …"`, `"Erratum for: …"`, `"Correction — …"`, the
  suffixes `"(Erratum)"` and `": publisher's note"`, and records with the
  OpenAlex type `erratum`/`retraction`. The original is matched by the title
  without the marker, and a close one (≥0.75) counts too: an erratum often
  repeats the wording of the translated edition. `for` counts as a separator
  only with a colon and a hyphen only between spaces; otherwise the papers
  `"Correction for motion artifacts…"` and `"Correction-based approach…"`
  would become notices. One title containing the other counts only on whole
  words and only when the shorter one has ≥4 words; otherwise
  `"Correction to: Introduction"` would attach to every paper with
  "introduction" in its title. A notice that does not match the other record
  of the pair does not reach the fuzzy buckets at all;
- `SERIES_NOT_VERSION`: `"Part I"`/`"Part II"`, `"Volume 2"`, a bare section
  number (`"... Applications. II. Applications"`, `"... industries. 2. ..."`,
  `"Lecture notes #2 on …"`), different roman numerals in the same position.
  A bare trailing number or roman numeral is a series only when both titles
  end in one and they differ (`"… COVID-19"` vs `"… COVID"` is not a series);
  an explicit marker (`Part`, `Volume`, `No.`) is enough on one side.
  Different numbers count as a series only when the text before the number
  matches (≥0.9); otherwise `"... Part 3"` and `"... Part 1"` of two
  unrelated series landed here;
- `SUPPLEMENT_OR_REVIEW`: `"Additional file N of …"`, `"Data from: …"`,
  `"Peer Review #…"` whose title actually tracks the other record's title
  (otherwise it is not a pair at all; else every deposit sharing an author
  would land here).

The types `software`/`dataset`/`peer-review`/`grant`/`other` reach neither
`MISSED_BY_NORM`, nor `DOI_SIBLING` (a `.N` component of such a type is
`SUPPLEMENT_OR_REVIEW`), nor the fuzzy buckets: software releases and data
deposits have their own versioning (repository dedup handles it), and a
paper-title heuristic says nothing about them.

## Running

```
pauk versions report [--output <path>]
```

Only reads the graph (`Neo4jClient.fetch_publications_for_dedup()`), needs no
MongoDB and takes no graph lock, since there is nothing to write to the
graph. The journal (`data/cache/version_candidates.jsonl` by default) is
line-delimited JSON, like `dedup_candidates_graph.jsonl`: `status`,
`entity`, `bucket`, `mergeable`, `record_a`/`record_b` and the signals the
decision was based on. Lines are ordered by the `(record_a, record_b)` pair,
so the journals of two runs can be diffed.

It is not part of `run`/`enrich`; run it by hand, like `pauk dedup graph`.

## Where this goes next

The first version is report-only. The "found → confirmed → merged" loop can be
closed in several ways later; none of them is implemented here:

- manual confirmation through `pauk admin merge` using `record_a`/`record_b`
  from the journal;
- moving `MISSED_BY_NORM`/`DOI_SIBLING` (deterministic, high precision) into
  the `dedup` stage itself as automatic merges;
- a level-B signal: `relation` from the Crossref response (`is-preprint-of`,
  `has-version`, `is-translation-of`) as authoritative confirmation instead
  of the heuristic for `PREPRINT_PUBLISHED`/`RU_EN_TRANSLATION`.
