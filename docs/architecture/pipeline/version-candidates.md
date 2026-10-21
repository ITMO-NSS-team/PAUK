# Publication versions: `IS_VERSION` and `pauk versions`

**What this covers:** why one work can be several Publication nodes, which
signals relate them, how MEDIUM_FUZZY is split further, and how a pair becomes
an `IS_VERSION` edge and a question in the review queue. Nothing here merges.

**Files involved:** `pauk/graph/version_candidates.py` (rules and buckets),
`pauk/graph/version_judge.py` (LLM verdict for MEDIUM_FUZZY),
`pauk/graph/version_link.py` (edges, `pauk versions link`); the CLI wiring is
in `pauk/cli.py`, the job in `pauk/jobs/worker.py`, the answers in
`pauk/storage/review.py` (`publication_version`) and `pauk/admin/review_routes.py`.

## Why this is separate from `dedup`

The `dedup` stage ([dedup.md](dedup.md)) folds publications that share a DOI,
and records with one title only where one of them has no DOI. Two records
under two DOIs are two nodes even with one title: a preprint and its article,
an original and its translation. What relates them is an `IS_VERSION` edge,
and the same goes for the cases where one work has several DOIs **and** a
different title:

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

## Why an edge, never a merge

`dedup` treats a name as evidence, not proof (see "Merge policy" in
`pipeline/stages/dedup.py`), and exactly the same holds for a title:
`"Cliques and Constructors in 'Hats' Game. I"` and `"... II"` have an almost
perfect title similarity and share their authors, yet they are two different
papers. And a version is not a duplicate even when it is right: a preprint, a
translation and a journal extension each have their own DOI, venue, text and
sometimes authors. So nothing here writes `merged_ids`: every version keeps
its node, and the pair becomes an `IS_VERSION` edge with the bucket on it. A
mergeable pair also becomes a `publication_version` question in the review
queue (`status: "held"` on the edge); a person confirms it (`"same"`) or
removes the edge for good (`"different"`).

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
| `MEDIUM_FUZZY` | similarity ≥0.78 · author overlap ≥0.50 or abstract similarity ≥0.45 · gap ≤4 years | the same, weaker: the noisiest bucket, split further below |

Title similarity is `max(word Jaccard, difflib.SequenceMatcher.ratio())` over
the title after NFKD normalization (diacritics removed, Cyrillic kept;
Cyrillic look-alikes of Latin letters inside a Latin word are replaced with
Latin ones) and removal of punctuation and stop words (English and Russian).
The first catches reordered words, the second point edits (a hyphen, an
ending). A Russian title and its English translation share no letters, so
such a pair is not found by the title alone.

Four buckets are not versions but explained look-alike pairs. They get an
edge too, but are never in `MERGE_BUCKETS` and never asked about:

- `ERRATUM`: `"Correction to: …"` / `"Erratum to: …"` / `"RETRACTED: …"` /
  `"Retraction notice to …"`, `"Erratum for: …"`, `"Correction — …"`,
  `"Publisher's Note: …"`, the suffixes `"(Erratum)"` and
  `": publisher's note"`, and records with the
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
  would land here);
- `COVER`: the publisher's record for an issue cover or a frontispiece, with
  the article's title and authors and a DOI of its own. `"Front Cover: …"`,
  `"Cover Feature: …"`, `"Frontispiece: …"`, the suffix
  `"(Adv. Mater. 13/2022)"`, a record of type `paratext`, or a rubric of up to
  four words before the other record's exact title in the same journal
  (`"Polymer Coatings: <title>"`). Checked before every bucket below it.

The types `software`/`dataset`/`peer-review`/`grant`/`other`/`paratext` reach neither
`MISSED_BY_NORM`, nor `DOI_SIBLING` (a `.N` component of such a type is
`SUPPLEMENT_OR_REVIEW`), nor the fuzzy buckets: software releases and data
deposits have their own versioning (repository dedup handles it), and a
paper-title heuristic says nothing about them.

## Splitting MEDIUM_FUZZY

172 hand-labelled MEDIUM_FUZZY pairs from a copy of the graph: 16 (9%) were
strictly one work (preprint, translation, duplicate record), 53 more a
conference paper and its journal extension, 62 sibling papers of one group, 29
different works, 12 covers. Title similarity does not separate them (median
0.80–0.84 in every class). `route_medium_fuzzy()` uses the signals that do,
in this order:

| Sub-bucket | Condition | Mergeable |
|---|---|---|
| `MEDIUM_FUZZY_STRONG` | abstract similarity ≥0.45, or a preprint in the pair (precision 0.81, no LLM call) | yes |
| `MEDIUM_FUZZY_LLM_SAME` | the LLM says one work (precision 0.94) | yes |
| `MEDIUM_FUZZY_CONF_JOURNAL` | the LLM says two works, but the pair is conference ↔ journal (precision 0.59: the usual journal extension of a talk) | yes |
| `MEDIUM_FUZZY_RELATED` | everything else: two conferences or two journals the LLM declined (precision 0.03) | no |
| `MEDIUM_FUZZY` | no verdict yet: run without `--llm`, or the call failed | yes |

Covers left this bucket for `COVER` (above). The venue kind comes from the
OpenAlex source type (`Publication.venue_type`), and a venue named like
proceedings (`conference`, `proceedings`, `symposium`, …) counts as a
conference whatever its type: OpenAlex types "Journal of Physics: Conference
Series" as a journal.

The model is the project's standard one (`PAUK_LLM_MODEL`), as for every
other LLM stage. The routing was measured with `qwen/qwen3-next-80b-a3b-instruct`:
precision 0.96 against 0.76 for `claude-haiku-4.5`, at $0.03 for the whole
bucket against $0.23; another standard model routes differently, so check
its split on a copy before trusting it. Haiku's extra recall is almost all "one result at two
conferences", which the policy counts as **two works**; the prompt says so.
The prompt sends both records' title, venue, type, date, DOI, up to 8 authors
and up to 1200 characters of abstract; every call is logged in Mongo
`llm_logs_version_candidates`. Every verdict, a "different" included, is kept
in Mongo `version_llm_verdicts` and reused by later runs, with or without
`--llm`, so the routing an earlier run paid for does not regress; `--force`
asks again, and a forced call that fails keeps the old verdict. Verdicts are
looked up through the publications' `merged_ids`, so a fold does not make a
paid-for pair new again.

## Running

```
pauk versions link [--llm] [--force]
pauk versions report [--output <path>]
```

`link` takes the graph lock, like `pauk dedup graph`, and rebuilds every
`IS_VERSION` edge from the current graph. With `--llm` the model is asked
first, before the lock: a few hundred sequential calls would otherwise keep
every publish waiting on Busy. Then:

- the edge points from the version to the work's face: the notice or the cover
  to its article, otherwise the record dedup's ranking would not keep
  (a preprint to the article, the older to the newer);
- edge properties: `bucket`, `mergeable`, `status` (`held` for a mergeable
  pair, `same` once confirmed, absent for a look-alike), `title_sim`,
  `llm_verdict`, `llm_reason`;
- a pair answered `"different"` gets no edge; an edge the rules no longer
  produce is removed, unless somebody added it by hand (`graph_overrides`,
  `op: "link"`) or confirmed it (`"same"`); an edge deleted by hand
  (`op: "delete"`) is not written again and its pair is not asked about;
- mergeable pairs become `publication_version` questions (the "Версии
  публикаций" tab of `/review`); an answer there changes the edge at once.
  An open question the rules no longer ask is dropped; an answered or
  skipped one stays.

The edges belong to this command: no prepared row carries them, so a publish
never writes them, and `pauk admin prune` leaves them alone
(`GRAPH_OWNED_RELATIONSHIPS`). A fold moves them onto the survivor and drops
an edge between the two halves of the fold rather than turning it into a loop.
The admin panel runs it as the `versions` job.

`report` only reads the graph, needs no MongoDB and takes no lock. It writes
the same pairs to the journal and leaves MEDIUM_FUZZY unrouted: the verdicts
live in Mongo. `link` writes the journal too.

The journal (`data/cache/version_candidates.jsonl` by default) is
line-delimited JSON, like `dedup_candidates_graph.jsonl`: `status`,
`entity`, `bucket`, `mergeable`, `record_a`/`record_b` and the signals the
decision was based on. Lines are ordered by the `(record_a, record_b)` pair,
so the journals of two runs can be diffed.

Neither is part of `run`/`enrich`; run them by hand, like `pauk dedup graph`,
after publishing.

## Records the old rule folded

Before DOI-only folding, dedup folded one title under two DOIs into one node
and kept the second record only as an entry of `Publication.versions`. That
ledger lacks what a node needs (type, fields, funding, departments), so the
record is rebuilt from its raw OpenAlex payload instead:
`scripts/split_publication_versions.py` takes the id out of the survivor's
`merged_ids` and `versions` (Mongo and graph) where `classify_pair` gives the
pair a bucket and the raw payload is still there (without it the ledger entry
is the last copy, and the record stays folded), and prints the `normalize` → `enrich` → `publish` → `prune` →
`versions link` sequence that recreates the record as its own node and runs
every stage, the code-link classifier included, on it. See
[../scripts.md](../scripts.md).

## Where this goes next

- a level-B signal: `relation` from the Crossref response (`is-preprint-of`,
  `has-version`, `is-translation-of`) as authoritative confirmation instead
  of the heuristic for `PREPRINT_PUBLISHED`/`RU_EN_TRANSLATION`;
- the map: versions are separate points today; drawing a confirmed cluster as
  one is left to `pauk/gui/` (the snapshot already carries `pub_versions`).
