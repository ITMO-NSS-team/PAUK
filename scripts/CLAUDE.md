# scripts/ — pipeline stages

Each file here is one idempotent stage of the OpenAlex → code_url pipeline,
runnable standalone via `uv run python scripts/<name>.py`. Order and full
command reference live in the root [CLAUDE.md](../CLAUDE.md).

## Local conventions

- **Config comes from `config.py` only.** Never hardcode paths, endpoints,
  hosts, batch sizes, or ITMO identifiers in a stage — add/read a constant in
  `config.py`. It also owns `.env` loading (via `load_dotenv`) and the
  `pdf_path_for()` / user-agent helpers.
- **One SQLite DB, shared schema.** All stages open `config.DB_PATH`. The
  schema is defined once in `init_db.py` (`SCHEMA_SQL`) — change table
  definitions there, and keep `PRAGMA foreign_keys = ON`.
- **Idempotency is a hard requirement.** Re-running any stage must not corrupt
  or duplicate saved data. Use `INSERT OR IGNORE` / upserts / `WHERE`-guards,
  and honor `--limit` for batched reprocessing.
- **Two user agents on purpose.** Normal requests use `config.USER_AGENT`
  (OpenAlex polite pool); `fetch_papers.py --retry-failed` switches to
  `config.BROWSER_USER_AGENT` to get past anti-bot PDF hosts. Keep that split.
- **LLM I/O is JSON-only.** `classify_repo_links.py` calls OpenRouter with
  `response_format: {"type": "json_object"}` and must tolerate malformed
  responses (it already catches `JSONDecodeError`/`KeyError`). Verdicts are
  written to `repo_links`; `sync_publications.py` is the only stage that
  aggregates them into `publications.has_code` / `code_url`.

## After editing

Run `graphify update .` from the repo root to refresh the graph (AST-only,
no API cost). There is no test suite — verify by re-running the affected stage.
