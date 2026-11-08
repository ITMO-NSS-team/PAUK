# `arxiv_versions` stage

**What this covers:** how a paper whose publisher PDF is out of reach gets an
arXiv copy to read, and why the stage runs before `code_links`.

**Files involved:** `pauk/pipeline/stages/arxiv_versions.py`,
`pauk/sources/semantic_scholar.py`.

Most ITMO papers are paywalled, and code links live almost only in the full
text. Many of those papers also exist as an arXiv preprint, but OpenAlex links
the preprint to the journal article for only part of them: the rest arrive
without any arXiv location in `pdf_urls`. Semantic Scholar keeps that pair for
many of them: a paper looked up by its journal DOI carries the preprint's id in
`externalIds.ArXiv`.

## What the stage does

It takes the publications that have a `doi`, no `full_text`, and no link to
`arxiv.org` in `pdf_urls`. Their DOIs go to
`POST /graph/v1/paper/batch?fields=externalIds`, 500 per request. For every
paper that comes back with an arXiv id, `https://arxiv.org/pdf/<id>` is
appended to the end of `pdf_urls`. The publisher's own links stay first, so
the preprint is read only when they fail, and `code_links` treats it like any
other PDF link. Each batch is written as it completes, and the response goes
to `raw` under the source `semantic_scholar`.

`normalize` merges `pdf_urls` with what is already stored, so the appended
link survives the next collect.

## Why before `code_links`

`code_links` marks a paper with no PDF candidates and nothing in the abstract
as `COMPLETED_EMPTY` and does not come back to it. So the stage runs first in
`ALL_STAGES`, and when it adds a link to a paper that `code_links` has already
processed, it drops that paper's `code_links` state. The same run then reads
the preprint.

## Statuses

- An arXiv link was added: `COMPLETED`, `result_count=1`.
- Semantic Scholar does not know the DOI, or knows it without an arXiv id:
  `COMPLETED_EMPTY`. It is not asked again without `--force`.
- The batch failed after retries: `FAILED` for every paper in it, with the
  error text; retried on the next run.

Papers outside the selection (no DOI, already have text, already have an arXiv
link) get no state at all: the check is cheap and is repeated every run.

## Limits and settings

The public pool shares about one request per second among all callers and
answers 429 often; a batch is retried up to eight times, following
`Retry-After`. With `SEMANTIC_SCHOLAR_API_KEY` the key goes into the
`x-api-key` header; `PAUK_SEMANTIC_SCHOLAR_RPS` (default 1) sets the pace.
Downloads from `arxiv.org` are paced at one every three seconds for every
client (`PUBLIC_LIMITS`), as arXiv asks of automated access.

An arXiv copy is usually the preprint, not the published version: its text,
page numbers and, occasionally, its links can differ from the journal's.
`full_text` and the page numbers of found links are then those of the
preprint.

Only papers with a DOI are looked up. A paper that Semantic Scholar has never
linked to arXiv stays without text.
