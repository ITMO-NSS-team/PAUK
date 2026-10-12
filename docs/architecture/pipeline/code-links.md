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
fragment, `raw_fragments` also keeps repeated spellings on the same page,
`candidate_urls` holds the alternatives of one finding, and `continuous` marks
an unbroken spelling in the visible text.

`_collect_occurrences` checks all pages of the same PDF. Only a full
`owner/repo` with no line breaks (compared case-insensitively) counts as
confirmation. The abstract, an annotation URI and a different way of joining
breaks do not resolve the ambiguity. If one variant appears continuously, it
stays; if both do, both stay; if neither, the alternatives are kept.

`repositories` requests each address once per run regardless of the number of
publications. The result is stored in `CodeLink.availability`: `available`,
`not_found` (HTTP 404), `failed` (other errors), or `unchecked` before the
check; the reason goes to `availability_error`. Availability does not prove a
reconstruction is right, so by itself it does not remove alternatives. Errors
keep being retried according to the usual repository statuses.

If an address has only ambiguous occurrences, `url_ambiguous=true`: it does not
enter `Publication.code_url` or `Repository.publication_ids`, even with a
positive relevance verdict. In the graph the alternatives are marked with the
properties `MENTIONS_LINK.url_ambiguous`, `candidate_urls` and `availability`;
the edge's `is_relevant` stays undefined.

## Text sources on one PDF page

`_pdf_page_occurrences()` combines two sources; text wins on a conflict:

1. The plain regex over `page.get_text()`.
2. **Real PDF hyperlinks** (`page.get_links()`, `kind == fitz.LINK_URI`): links
   whose visible text does not show the URL (for example the word "here"
   pointing to a repository). The context for such a link is the visible text
   of the clickable area itself (`_annotation_context`), not the surrounding
   page text. If the same URL was already found through plain text on that
   page, the annotation is ignored: the text context is more informative than
   a "here" label.

## Fallback through PDF-Crawler-Service

Unique `pdf_urls` are tried first: `best_oa_location.pdf_url`, then the
non-empty `locations[].pdf_url` in source order. If all direct links failed to
download or parse (or there are none), but the publication has a `doi` and
`PAUK_PDF_CRAWLER_URL` is set, the stage probes `GET {url}/health` once per
stage run (not per publication, no retries); on success, for such publications
it calls `GET {url}/download?url=<urlencoded doi>`. Service details and
configuration: [../deploy.md](../deploy.md).

## Download and cache

`data/pdf/<publication_id>.pdf` (`PdfStore`, with a `{fetched_at}` pointer in
the `pdfs` collection). The write is atomic (temporary file plus
`os.replace`) and happens only after the PDF parsed successfully, so HTML error
pages never reach the cache. If an old cached file cannot be read, direct links
and the crawler are tried, and a successful PDF replaces the corrupt file. A
valid cache is not downloaded again. The raw text of each page (without
ligature normalization, which is applied separately and only for matching) is
joined into `Publication.full_text` with `"\n\n"`.

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
and no verdict. The next stage, `link_relevance`, sends the model **all** saved
occurrences of one link in a single request: the abstract and each PDF context
are labeled with their source. The response is validated strictly:

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
included. The Zenodo archived repository is the one deterministic exception:
`classified`, `true`, confidence `1.0`, reason
`"repository_archived_by_this_deposit"`, with no model call.

After classification, `has_code=true` only if at least one
`CodeLink.is_relevant=true` exists. `code_url` holds a JSON list of the URLs of
all such links in discovery order; it is a string field to preserve the
prepared/Neo4j contract, and the GUI generator decodes it into a plain list.
Third-party and undetermined links are not in the list. If at least one model request failed, the
publication-level `has_code` and `code_url` keep the last fully classified
state until a successful retry.
