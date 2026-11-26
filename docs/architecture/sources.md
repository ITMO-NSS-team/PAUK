# `pauk/sources/`: HTTP clients for external APIs

**What it covers:** the shared HTTP layer (retry and backoff) and one client
per external API.

**Files:** `pauk/sources/base.py`, `rate_limit.py`, `openalex.py`, `github.py`,
`crossref.py`, `orcid.py`, `semantic_scholar.py`, `llm.py`, `pdf.py`.

API wrappers inherit from `base.py::HttpClient`; `SubscriptionCliClient` runs
a local CLI. `PdfDownloader` manages a separate HTTP client per download thread.

## `base.py::HttpClient`

- `get_json(url, params=None, retries=3)` and `request_json(method, url,
  ...)` return parsed JSON; `get_bytes(url, retries=3, timeout=None)` returns
  the raw body (used to download PDFs in `code_links.py`); `get_text(...)`
  returns text.
- All of them go through `_request`, the shared retry loop, which makes up to
  `retries + 1` attempts. A status in `RETRYABLE_STATUS_CODES` (`429`, `500`,
  `502`, `503`, `504`) waits for the server's `Retry-After` header if it sent
  one, otherwise an exponential delay (`min(60, 2**attempt)`). Network
  exceptions (`requests.RequestException`) use the same exponential delay.
  When retries run out, or on any other error status, it raises
  `HttpRequestError` with a redacted URL.
- A wait of 10 seconds or more is logged as one warning before the sleep: how
  long, until when, the redacted request and the reason. Shorter waits stay
  silent. `OpenRouterClient` has its own retry loop and logs its waits the
  same way.
- The optional `cancel` event interrupts waits for retries or host limits.
- `retries` is a parameter so a caller can tune how aggressive retrying is.
  For example, the crawler health check in `code_links.py` runs with
  `retries=0`, so a dead service does not delay the whole run.

With `deadline` or `max_wait`, `get_bytes` and `get_bytes_with_url` stream the
body inside the same retry loop. Read errors retry the request; the host slot
stays occupied until reading ends. `cancel()` stops reads and waits; `close()` also
closes the session. PDF timeouts and cancellation limits are documented in
[code-links.md](pipeline/code-links.md#download-and-cache).

## Request limits

`base.py::RATE_LIMITER` is one `rate_limit.py::HostRateLimiter` shared by
all HTTP clients in the process. It spaces requests without catch-up bursts
and caps concurrency per host. Retries and redirects from a limited API
request each pass through the limiter. Separate processes are not coordinated.
PDF requests share publisher rates and the lowest cap of the active downloaders.
Closing a downloader removes its cap. Server limits and pauses use the common
limiter, including on redirects. Server ceilings stay in effect until restart.
Each PDF request takes a slot only when both limiters admit it.

Default profiles (`pauk/settings.py`):

| Host | Access | Requests/s | Maximum concurrent requests |
|---|---|---:|---:|
| `api.openalex.org` | API key | 30 | controlled by the caller's workers |
| `api.openalex.org` | anonymous | 10 | controlled by the caller's workers |
| `pub.orcid.org` | public | 12 | controlled by the caller's workers |
| `api.crossref.org` | contact email supplied | 10 | 3 |
| `api.crossref.org` | anonymous | 5 | 1 |
| `api.semanticscholar.org` | with or without a key | 1 | 1 |
| `arxiv.org`, `www.arxiv.org`, `export.arxiv.org` | public, every client | 1/15 | 1 |

`OpenAlexClient` sends the key as `Authorization: Bearer ...`.
`SemanticScholarClient` sends its optional key as `x-api-key` to Semantic
Scholar only. Public arXiv PDF downloads in `code_links` use no separate
arXiv API key. Local `PUBLIC_LIMITS` profiles space request starts by 15 seconds
for each of the three hosts, shared by all clients in the process. The main
site's [robots.txt](https://arxiv.org/robots.txt) (also
[www](https://www.arxiv.org/robots.txt)) allows `/pdf` with wildcard `Crawl-delay: 15`,
not the legacy API's three-second rule. [Export's robots.txt](https://export.arxiv.org/robots.txt)
has wildcard `Disallow: /`, despite the [bulk-data guide](https://info.arxiv.org/help/bulk_data.html)
recommending export. Its local profile is not permission to crawl or a new faster
route. Host aliases and separate processes do not share a single budget; see
[arxiv-versions.md](pipeline/arxiv-versions.md#limits-and-settings).
`CrossrefClient` sends `mailto` in the query and `User-Agent` so redirects
retain polite-pool access. Requests handles authorization on redirects.

On `429` or `503`, a shared host pause uses the existing retry delay parser
(`Retry-After` seconds or HTTP date, otherwise exponential backoff). Other
retryable statuses also create a shared pause when they carry `Retry-After`.
The pause also applies after the current caller exhausts its retries;
it overlaps the caller's normal backoff.
When a response starts or extends a pause of at least 10 seconds, the
limiter logs which host must wait and for how long. The warning appears
even if the current request has no retries left.

`X-Rate-Limit-Limit` together with `X-Rate-Limit-Interval` in seconds, and
`X-Concurrency-Limit`, can lower the host's ceilings. Malformed values are
ignored. OpenAlex's daily budget headers are not interpreted as requests/s;
the limiter does not track daily quotas.

Settings:

| Environment variable | Default | Meaning |
|---|---|---|
| `PAUK_OPENALEX_RPS` | `30` | OpenAlex requests/s; capped at 10 without a key |
| `PAUK_ORCID_RPS` | `12` | ORCID requests/s |
| `PAUK_CROSSREF_RPS` | `10` | Crossref requests/s; capped at 5 without a contact |
| `PAUK_CROSSREF_CONCURRENCY` | `3` | Crossref requests in flight; one without a contact |
| `CROSSREF_MAILTO` | empty | Contact email for Crossref polite-pool access |
| `ORCID_CLIENT_ID` / `ORCID_CLIENT_SECRET` | empty | Set both for registered Public API access, leave both empty for anonymous reads |
| `SEMANTIC_SCHOLAR_API_KEY` | empty | Semantic Scholar key; without it the shared public pool is used |
| `PAUK_SEMANTIC_SCHOLAR_RPS` | `1` | Optional Semantic Scholar pace override: lower it or match a higher quota assigned to the key |

## GitHub limits

GitHub can report GraphQL throttling in an HTTP 200 response. Both APIs reuse
the HTTP retry budget. An exhausted primary quota waits for `X-RateLimit-Reset`
on that API. Secondary throttling pauses REST and GraphQL in the process. It
uses `Retry-After`, the reset time if the quota is exhausted, or exponential
backoff starting at one minute. Successful entries from a partial GraphQL
response survive failed retries.

## Clients

| File | Class | What it provides |
|---|---|---|
| `openalex.py` | `OpenAlexClient` | `get_work`, `iter_works` (cursor pagination by ROR and date range), `get_author`, `get_authors` |
| `github.py` | `GitHubClient` | `prefetch_repositories` and `get_users` batch metadata and profiles via GraphQL with a token. REST supplies individual lookups, README fallback, contributor lists, commit history and repositories of an owner. |
| `crossref.py` | `CrossrefClient` | `get_work(doi)`, used by `PersonsStage` to backfill ORCID by surname; optional contact email selects polite-pool limits |
| `orcid.py` | `OrcidClient` | `get_record(orcid)`. Sessions with the same credentials share a `/read-public` token until expiry or HTTP 401. |
| `semantic_scholar.py` | `SemanticScholarClient` | `papers_by_doi(dois)`: up to 500 DOIs per `POST /graph/v1/paper/batch`, one record or `None` per DOI in request order, retried up to eight times; `arxiv_id(record)` reads `externalIds.ArXiv`. Used by `ArxivVersionsStage` |
| `llm.py` | `OpenRouterClient` | `chat_json(...)`: OpenRouter chat completions in JSON-object mode. `last_usage` and `last_response` hold the usage and raw body of the latest call. `usage()`: dollars the key has spent so far, read by `pauk reports build` before and after a run |
| `llm.py` | `SubscriptionCliClient` | The same `chat_json(...)` through a local subscription CLI: `ClaudeCli` runs `claude -p`, `CodexCli` runs `codex exec`. `llm_client(provider, ...)` picks the client from a provider setting (`openrouter`, `claude:<model>`, `codex:<model>`; a bare CLI name gets `Settings.claude_cli_model` or `Settings.codex_cli_model`), `check_providers` parses every stage's setting and looks for its CLI when `run`, `enrich` or `dedup` starts, `kill_running_clis` kills the CLIs of an interrupted stage and refuses new ones until `resume_clis`. A CLI reports a spent limit with `SubscriptionLimitError` and a lost login or 20 failures in a row with `CliUnusableError`; both stop the command |

## Logging

`OpenRouterClient` logs individual requests at DEBUG. Repeated failures are
throttled per kind: the first and every hundredth occurrence are warnings;
the rest remain visible with `--verbose`. Use stage summaries and progress
for INFO-level monitoring. File and container log formatting and redaction
are configured by `pauk/logging.py`.

## PDF-Crawler-Service is not in this package

Fallback PDF download by DOI through the external
[PDF-Crawler-Service](https://github.com/gurinboru/PDF-Crawler-Service) lives
in `pipeline/stages/code_links.py`,
not here. `pdf.py::PdfDownloader` manages thread-local HTTP clients and shutdown.
See [pipeline/code-links.md](pipeline/code-links.md) and [deploy.md](deploy.md).
