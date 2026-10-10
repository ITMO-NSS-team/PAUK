# `pauk/sources/`: HTTP clients for external APIs

**What it covers:** the shared HTTP layer (retry and backoff) and one client
per external API.

**Files:** `pauk/sources/base.py`, `openalex.py`, `github.py`, `crossref.py`,
`orcid.py`, `llm.py`.

Thin wrappers, each knowing only its own API. All inherit from
`base.py::HttpClient`.

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
- `retries` is a parameter so a caller can tune how aggressive retrying is.
  For example, the crawler health check in `code_links.py` runs with
  `retries=0`, so a dead service does not delay the whole run.

## Clients

| File | Class | What it provides |
|---|---|---|
| `openalex.py` | `OpenAlexClient` | `get_work`, `iter_works` (cursor pagination by ROR and date range), `get_author` |
| `github.py` | `GitHubClient` | `get_repository`, `has_readme` (a separate call: the repository payload does not say whether a README exists), `contributors`, `commits`, `get_user`, `user_repositories`. It also retries `403` responses that are rate limits, honouring `Retry-After` and `X-RateLimit-Reset` |
| `crossref.py` | `CrossrefClient` | `get_work(doi)`, used by `PersonsStage` to backfill ORCID by surname |
| `orcid.py` | `OrcidClient` | `get_record(orcid)` |
| `llm.py` | `OpenRouterClient` | `chat_json(...)`: OpenRouter chat completions in JSON-object mode. `last_usage` and `last_response` hold the usage and raw body of the latest call |

## PDF-Crawler-Service is not in this package

Fallback PDF download by DOI through the external
[PDF-Crawler-Service](https://github.com/gurinboru/PDF-Crawler-Service) lives
in `pipeline/stages/code_links.py` (`_crawler_available` and `_pdf_pages`),
not here. It is stage-specific logic on top of the ordinary `HttpClient`, not
a general-purpose client. See [pipeline/code-links.md](pipeline/code-links.md)
and [deploy.md](deploy.md).
