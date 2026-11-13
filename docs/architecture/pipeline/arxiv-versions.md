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

It takes the publications that have a `doi`, no `full_text` (empty or only
whitespace), and no link to `arxiv.org` in `pdf_urls`. On the first collect of
a group nothing has text yet, so every paper with a DOI is looked up, open
access ones included; for them the extra link is never reached.

The DOIs go to `POST /graph/v1/paper/batch?fields=externalIds`, 500 per
request. For every paper that comes back with an arXiv id,
`https://arxiv.org/pdf/<id>` is appended to the end of `pdf_urls`. The
publisher's own links stay first, and `code_links` tries `pdf_urls` before the
PDF-Crawler-Service, so the preprint is read when the publisher's links fail
and before the crawler is asked. Each batch is written as it completes, and
the response goes to `raw` under the source `semantic_scholar`.

`normalize` merges `pdf_urls` with what is already stored, so the appended
link survives the next collect.

## Handing the paper to `code_links`

The stage runs before `code_links` in `ALL_STAGES`. A paper that `code_links`
has already processed would not be read again, because it ended
`COMPLETED_EMPTY` with no PDF candidates. When the stage adds a link to a paper
that has no code links in `repo_links`, it drops that paper's `code_links`
state, and the same run reads the preprint.

A paper that already has links keeps its `code_links` state. A re-run of
`code_links` rebuilds the links from the abstract and the PDF, which would
discard links added by hand and the `link_relevance` verdicts already paid
for. Its arXiv link waits for `pauk enrich code_links --force`.

## Statuses

- An arXiv link was added: `COMPLETED`, `result_count=1`.
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

A cached PDF in `data/pdf/` is read before any link, so a paper whose cached
file is a scan without a text layer gets the arXiv link but `code_links` keeps
reading the scan.
