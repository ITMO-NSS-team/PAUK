# `arxiv_versions` stage

**What this covers:** how a paper without full text gets an arXiv copy to
read, and how the stage hands it to `code_links`.

**Files involved:** `pauk/pipeline/stages/arxiv_versions.py`,
`pauk/sources/semantic_scholar.py`.

Most ITMO papers are paywalled, and code links live almost only in the full
text. Many of those papers also exist as an arXiv preprint, but OpenAlex links
the preprint to the journal article for only part of them. Semantic Scholar
keeps that pair for many of the rest: a paper looked up by its journal DOI
carries the preprint's id in `externalIds.ArXiv`.

## What the stage does

It takes publications that have a non-arXiv DOI, no `full_text` (empty or
only whitespace), and no recognized arXiv link in `pdf_urls`. On the first
collect of a group nothing has text yet, so this includes open-access papers.

The DOIs go to `POST /graph/v1/paper/batch?fields=externalIds`, 500 per
request. For every paper that comes back with an arXiv id,
`https://arxiv.org/pdf/<id>` is appended to the end of `pdf_urls`. The
publisher's own links stay first. Each batch is written as it completes, and
the response goes to `raw` under the source `semantic_scholar`. A native
arXiv DOI with no PDF URL gets the canonical primary URL directly, without
looking it up in Semantic Scholar.

## Handing the paper to `code_links`

`arxiv_versions` runs before `code_links`. It records each discovered URL in
`arxiv_sources` with its own extraction state, which survives normalize and
publication deduplication. `code_links` reads pending sources even when the
paper already has links or a publisher PDF. A PDF failure leaves that source
eligible for retry without discarding text or links. If the paper has no old
code links, discovery also reopens the ordinary extraction path.

A journal paper can borrow a cached PDF from a unique prepared preprint only
when the cache's recorded source and byte digest verify the arXiv identity.
An explicit revision requires an exact version match; an unversioned URL
does not prove a revision. Old cache entries and crawler responses without
verified provenance do not qualify. When borrowing isn't possible, the stage
requests the arXiv PDF directly and keeps it in a separate source cache if it
cannot safely use the preprint's publication cache. Occurrences keep their
source PDF ID and local page number. Preprint text fills a gap rather than
replacing existing text.

New occurrences use the ordinary repository URL identity. With `--force`,
`code_links` removes stale automatic occurrences from successfully reread
PDFs, not from sources it couldn't read. Manual entries recorded without
occurrences keep their verdict and are not folded into automatic evidence.
Links without evidence changes keep their verdicts; newly found or changed
automatic evidence goes to `link_relevance`. Older appended URLs are not
backfilled as separately tracked sources.

## Statuses

- Semantic Scholar returned an arXiv link: `COMPLETED`, `result_count=1`.
- Semantic Scholar does not know the DOI, or knows it without an arXiv id:
  `COMPLETED_EMPTY`. It is not asked again without `--force`.
- The batch failed after retries, or the response did not match the request:
  `FAILED` for every paper in it, with the error text; retried on the next run.

Papers outside the selection get no state: the check is cheap and is repeated
every run.

## Limits and settings

`SEMANTIC_SCHOLAR_API_KEY` is optional and goes into the `x-api-key` header
for Semantic Scholar requests only. Without it, requests share the public
pool with other unauthenticated users. The default pace is 1 request/s,
matching Semantic Scholar's introductory key quota. The advanced setting
`PAUK_SEMANTIC_SCHOLAR_RPS` can lower that pace or match a higher quota
assigned to the key; it does not change the server's quota. See
[Semantic Scholar's API guide](https://www.semanticscholar.org/product/api/tutorial).

A batch is retried up to eight times, following `Retry-After` when provided
and using exponential backoff otherwise. A batch can take a few minutes,
and a stop request is noticed between batches. A local rate limit does not
guarantee that the shared public pool will accept a request.

The stage queries Semantic Scholar, not the arXiv API. `code_links`
downloads public arXiv PDF URLs without a separate arXiv API key. The
project's `PUBLIC_LIMITS` pace requests to each of `arxiv.org`,
`www.arxiv.org` and `export.arxiv.org` at one every 15 seconds, with one
request in flight per host, shared by all HTTP clients in the process.
Spacing is start-to-start: a transfer lasting longer than 15 seconds does not
incur another 15-second wait. Limits are per host and per process, not a
single budget shared across host aliases or separate processes.

The main site's [robots.txt](https://arxiv.org/robots.txt) (also
[www](https://www.arxiv.org/robots.txt)) allows `/pdf` with wildcard
`Crawl-delay: 15`; the legacy API's three-second rule is not the PDF rule.
[Export's robots.txt](https://export.arxiv.org/robots.txt) instead has wildcard
`Disallow: /`, conflicting with the [bulk-data guide's](https://info.arxiv.org/help/bulk_data.html)
recommendation to use export. The local 15-second profile for all three hosts
is not permission to crawl export, nor does it introduce a faster download route.

An arXiv copy is usually the preprint, not the published version: its text,
page numbers and, occasionally, its links can differ from the journal's.

A cached publisher PDF, including a scan with no text layer, does not block
extraction of a separately tracked arXiv source.
