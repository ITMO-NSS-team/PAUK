# `code_links` stage

**What this covers:** how a publication gets its list of GitHub repository
links: text sources, the regex and its defenses against PDF text-extraction
artifacts, the cache, and the fallback through an external service.

**Files involved:** `pauk/pipeline/stages/code_links.py`.

Looks for mentions of GitHub repositories in the abstract (always), in the full
PDF text (if a PDF could be obtained), and, for archived Zenodo deposits, in
the title.

## Link sources and their order

1. **Archived repository of a deposit** (`_archived_repository_url`): Zenodo
   mints a DOI for every GitHub release, and OpenAlex indexes it as a separate
   work of type `software` or `dataset` with a title like
   `"asl/BandageNG: Continuous build"`. `REPOSITORY_ARCHIVE` parses
   `owner/name:` at the start of the title (a space after the colon is
   required, which rules out prose like `"A/B testing: results"`). If it
   matches, this link goes first, before the abstract: for a deposit it is not
   a "mention", it is the point of the record.
2. **Abstract**: the same regex as for PDF pages, see below.
3. **PDF, page by page**: page text plus real PDF hyperlinks (annotations), see
   below.

All sources are merged in `_collect_occurrences()`: one `CodeLink` per
canonical URL, with `occurrences: list[LinkOccurrence]` listing every place it
appeared. The order is stable: archive, abstract, then PDF pages.
`code_links` itself no longer treats a found link as author code and does not
set `has_code`/`code_url` from it; the next stage, `link_relevance`,
recomputes those fields. A repeat `code_links` run removes its old processing
status, because a verdict on the earlier contexts is invalid once the
occurrence list is replaced.

## The regex `GITHUB_URL`

```python
_WRAP = r"(?:-\n[ \t]*|(?<=_)\n[ \t]*)?"
_CHAR = r"(?:-(?!\n)|[\w.])"
_SEGMENT = _CHAR + r"+(?:" + _WRAP + _CHAR + r"+)*"
GITHUB_URL = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.)?github" + _WRAP + r"\.com" + _WRAP + r"/"
    + _SEGMENT + r"/" + _SEGMENT, re.IGNORECASE)
```

- **The scheme is optional**: it catches a bare `github.com/org/repo` in prose,
  not only `https://...`. The lookbehind `(?<![\w.])` stops a match inside a
  longer host (`mygithub.com` and `sub.github.com` do not match).
- **A hyphenated line break is recognized, but only inside an already started
  match**: `_WRAP` is built into the pattern's segments rather than applied as
  preprocessing of the whole text. A break is handled only while the regex
  engine is still parsing `github.com/owner/repo`, not after it. The next
  line's continuation may be ordinary text, so the joined result is kept as a
  candidate.
- **A break after `_` is also recognized**: both the short address and the one
  with the next line's continuation are kept. Other ordinary line breaks are not
  joined; blank lines and page boundaries are not crossed.
- **A deep path collapses to `owner/repo`** (`_canonical_github_url`):
  `.../org/repo/blob/main/README.md` becomes `.../org/repo`. This matters for
  PDF hyperlinks: unlike regex matches in text (which cannot go beyond two
  segments, since `/` is not in the character class), a real annotation `href`
  can point anywhere inside a repository.
- **PDF font ligatures are normalized** (`_normalize_ligatures`,
  `unicodedata.normalize("NFKC", ...)`) before matching: some fonts render
  `ff`/`fi`/`fl` as a single Unicode character, which `\w` matches as an
  ordinary letter. Without normalization the same link with and without a
  ligature would become two different URLs.
- **A tail glued on without a separator is trimmed** (`_GLUED_TAIL`): a PDF
  sometimes renders the next sentence or a footnote number right against the
  link with no space. `repo.We evaluate...` becomes `repo`, `repo.12 citations`
  becomes `repo`. A bare digit without a dot (`repo1`) is left alone, since a
  GitHub repository can legitimately end in a digit (`detectron2`).

## Alternatives at line breaks

`_url_candidates` keeps both readings of `-\n` (with and without the hyphen),
and for `_\n` the address with and without the continuation. Several breaks
produce combinations; beyond 32 variants PDF extraction fails with an error
instead of truncating silently. `LinkOccurrence.raw_url` holds the original
fragment, `raw_fragments` holds fragments of that occurrence, `candidate_urls`
holds its alternatives, and `continuous` marks an unbroken spelling in the
visible text. Repeated spellings of one URL on the same page remain separate
`LinkOccurrence` entries; `text_start`/`text_end` retain their page-text offsets.
`pdf_id` identifies the source PDF, and `page_number` stays local to that file,
including PDFs of merged versions.

`_collect_occurrences` checks all pages of the same PDF. Only a full
`owner/repo` with no line breaks (compared case-insensitively) counts as
confirmation, including in the bibliography: a `reference` source still
represents visible text. Confirmation is scoped to one `pdf_id`. The abstract,
an annotation URI and a different way of joining breaks do not resolve the
ambiguity. If one variant appears continuously, it stays; if both do, both
stay; if neither, the alternatives are kept.

`repositories` requests each address once per run regardless of the number of
publications. The result is stored in `CodeLink.availability`: `available`,
`not_found` (HTTP 404), `failed` (other errors), or `unchecked` before the
check; the reason goes to `availability_error`. Availability does not prove a
reconstruction is right, so by itself it does not remove alternatives. Errors
keep being retried according to the usual repository statuses.

If an address has only ambiguous occurrences, `url_ambiguous=true`: it does
not enter `Publication.code_url` or `Repository.publication_ids`.
`link_relevance` skips the model call and stores a deterministic undetermined
result (`classified`, `is_relevant=null`, reason `"ambiguous_url_extraction"`).
In the graph the alternatives are marked with `MENTIONS_LINK.url_ambiguous`,
`candidate_urls` and `availability`; the edge's `is_relevant` stays undefined.

## Text sources on one PDF page

`_pdf_page_occurrences()` combines two sources:

1. The plain regex over `page.get_text()`.
2. **Real PDF hyperlinks** (`page.get_links()`, `kind == fitz.LINK_URI`): links
   whose visible text does not show the URL (for example the word "here"
   pointing to a repository). `_annotation_context` locates the clickable
   label and includes the surrounding page-text window. If the label occurs
   several times, the match nearest the annotation rectangle is selected.
   Like `Page.search_for()`, matching ignores ASCII case so hit positions stay
   aligned. If label matching fails, words at least half covered by the
   annotation rectangle recover the location; words are extracted at most
   once per page. If neither method works, only the label is saved rather
   than a potentially wrong window. An annotation remains a separate
   occurrence even when the same URL is visible in the text: deduplicating
   one source must not erase the other.

## Numbered footnotes and bibliography

After all pages are extracted, `_link_reference_contexts()` finds
`References`/`Bibliography`/`Литература`/`Список литературы`, parses entries
labeled `[9]`, `9.` or `9)`, and connects an entry's GitHub URL with each
in-text use of `[9]`. Ranges and lists (`[8-10]`, `[8, 9; 10]`) are expanded.
Both pieces of evidence survive: the entry with `source=reference` and the
in-text window with `source=citation`; `reference_label` stores the number.
`Appendix`, `Supplementary`, `Supporting Information` and their Russian
equivalents end the bibliography, so later text is treated as body text again.
Lettered headings such as `A Comment Extraction` are recognized too, even
when the letter and title are extracted on separate lines. An entry's number
may occupy its own line and an entry may continue onto the next page. Bare
numbers require confirmation by an in-text citation or sequential numbering
so years and page numbers do not become reference labels. An unnumbered
bibliography retains `reference` without `reference_label`, with no attempt
to connect it to numeric citations.

A numbered footnote with no heading is recognized conservatively: the same
marker must already appear higher on the same page. This avoids treating an
ordinary numbered list with a URL as footnotes. Footnote labels are scoped
to their page so equal numbers from different pages do not mix.

Superscript markers (`¹` or an ordinary digit raised by the PDF font) are
recognized by `_pdf_footnotes()` using position, size and font flags, not by
searching for a bare digit in flattened text. A marker after a word is linked
to the same number in a smaller font lower on the page; text-block boundaries
and the next footnote bound the entry. Exponents after single variables and
italic mathematical fragments are excluded. On the first page, numbers before
`Abstract`/`Аннотация` are not linked because they are often author
affiliations. Superscript `¹` and bibliographic `[1]` use separate label
namespaces. The normalized source text and offsets do not change; ambiguous
URL alternatives survive in added citation contexts too. Font analysis only
runs on pages with repository links. This is conservative: cross-page
footnotes and scans without a text layer are not recovered.

## Fallback through PDF-Crawler-Service

Unique `pdf_urls` are tried first: `best_oa_location.pdf_url`, then the
non-empty `locations[].pdf_url` in source order. If all direct links failed to
download or parse (or there are none), but the publication has a `doi` and
`PAUK_PDF_CRAWLER_URL` is set, the stage probes `GET {url}/health` once per
stage run (not per publication, no retries); on success, for such publications
it calls `GET {url}/download?url=<urlencoded doi>`. Service details and
configuration: [../deploy.md](../deploy.md).

## Parallel downloads

Downloading is nearly all of the stage's time: on UFJF one thread fetched
about five PDFs a minute, publisher after slow publisher. `_fetch_all`
downloads and extracts `PAUK_CODE_LINKS_CONCURRENCY` publications at once
(8 by default, each thread with its own `HttpClient`) and hands them back in
order; finding the links and writing the states stays in the main thread.

Each download also has a whole-body deadline (`PDF_DOWNLOAD_DEADLINE`,
300 s; the crawler's 180 s). The request timeout bounds a single socket
read, so a server that trickles a few bytes at a time never trips it: on UFJF
one such repository held a thread, and with it the ordered hand-back, for
over half an hour. The deadline is enforced by a timer that shuts the socket
down (`HttpClient._abort_read`), not by a check between chunks: a buffered read
waits for a whole 64 KB chunk, and on KAIST one Korean journal's server took
three hours to fill one. A host that asks to wait longer than `PDF_MAX_RETRY_WAIT`
(120 s) before a retry is given up on: one journal's `Retry-After: 3600`,
honoured three times, cost the HSE run three hours. A direct PDF link gets one
retry (`PDF_RETRIES`) instead of the client's default three.

## Download and cache

`data/pdf/<publication_id>.pdf` (`PdfStore`, with a `{fetched_at}` pointer in
the `pdfs` collection). The write is atomic (temporary file plus `os.replace`)
and happens only after the PDF parsed successfully, so HTML error pages never
reach the cache. If an old cached file cannot be read, direct links and the
crawler are tried, and a successful PDF replaces the corrupt file. A valid
cache is not downloaded again. Normalized page texts are joined into
`Publication.full_text` with `"\n\n"`.

After the primary PDF, the stage reads already-cached files from `merged_ids`
and `versions[].openalex_id`, once per id. Versions cause no extra network
downloads. If the primary PDF is absent but a version cache is readable, it is
used without trying to download another primary PDF. If all version caches
are corrupt, the normal primary sources are still tried; the version error
remains explicit and its earlier occurrences are not lost.

Texts are combined in `full_text`, and every occurrence keeps its own `pdf_id`
and local page number. Caches are read even without `pdf_urls`. Failure or
disappearance of a previously used version PDF preserves its old occurrences
and sets `failed`; evidence from a successfully reread PDF is replaced rather
than mixed with stale evidence. URL spellings differing only by HTTP/HTTPS,
case or cosmetic suffixes are combined into one `CodeLink` with all occurrences.

## Statuses

- No PDF candidates and (the crawler is off, or the publication has no `doi`):
  the stage still works from the abstract, `COMPLETED` or `COMPLETED_EMPTY`
  depending on whether links were found.
- PDF download or parsing failed (from `pdf_urls` or the crawler): `FAILED`
  with the error text in `error`; the abstract result is not lost. If a PDF was
  read successfully before, its `LinkOccurrence` entries are kept until a
  successful retry: a temporary 403 or timeout must not erase evidence
  collected earlier. It is retried on the next `enrich` run even without
  `--force` (`FAILED` is among the retried statuses).
- A crawler health check that itself fails does not move publications to
  `FAILED`: it disables the fallback for that run and counts as "nothing to
  try".

## `is_relevant`, `llm_confidence`, `llm_reason`

`code_links` leaves ordinary found links with classification status `pending`
and no verdict. The next stage, `link_relevance` (publications in
`PAUK_LINK_RELEVANCE_CONCURRENCY` threads, 8 by default), sends up to 12 unique contexts
of one link per request. At the limit, at least one context of each available
source type is kept first, then different PDFs/pages are prioritized, then
remaining slots are filled in discovery order. Windows of the same source,
reference label, PDF and page overlapping by at least 80% of the smaller
window are not repeated in the request; they remain in prepared data. The
overlap threshold is a heuristic against many partial annotations of one link.

The limit is heuristic, not imposed by the model: each occurrence contains up
to 500 characters on either side, so 12 contexts keep the evidence near 12,000
characters while leaving room for all six source types and repeated
confirmations. All occurrences remain in prepared data and the full exported
context. The abstract and each PDF context are labeled with their source.
The prompt and source labels are English; original evidence is not translated.
The response is validated strictly:

- `is_relevant=true`: the repository was published by the authors of the
  publication;
- `is_relevant=false`: it is a third-party tool or an ordinary link;
- `is_relevant=null`: the model answered successfully, but the context is
  insufficient or contradictory.

`CodeLink.classification_status` separates `pending`, `classified` and
`failed`, so a successful undetermined answer (`classified` plus
`is_relevant=null`) is not confused with a link not yet processed or with a
failed call. An explicit undetermined decision stores `llm_confidence` and
`llm_reason`; an error clears the earlier verdict, sets `failed` and leaves the
stage in `FAILED`, so the next run repeats the request. The stage's
`result_count` counts all links with status `classified`, undetermined ones
included. Two cases are deterministic and need no model call:

- the Zenodo archived repository: `classified`, `true`, confidence `1.0`,
  reason `"repository_archived_by_this_deposit"`;
- a URL with only ambiguous occurrences: `classified`, `null`, no confidence,
  reason `"ambiguous_url_extraction"`.

After classification, `has_code=true` only if at least one unambiguous
`CodeLink.is_relevant=true` exists. `code_url` holds a JSON list of those URLs
in discovery order. HTTP/HTTPS, case, `www.`, trailing slash and `.git` do not
create duplicates; the first spelling is retained. It is a string field to
preserve the prepared/Neo4j contract, and the GUI generator decodes it into a
plain list while still accepting the legacy single-string form. Third-party
and undetermined links are not in the list. If at least one model request
failed, publication-level `has_code` and `code_url` keep the last fully
classified state until a successful retry.
