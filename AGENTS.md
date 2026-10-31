# AGENTS.md

Instructions for an agent, and for a human opening this repository for the
first time. Rules and pointers live here; how the system actually works
lives in `docs/`, not duplicated here.

## What this project is

PAUK collects ITMO's publications from OpenAlex, enriches them (Crossref,
ORCID, GitHub), finds code links in them, and loads the result
into Neo4j. `pauk/gui/` renders an interactive map of that graph.

One Python package, `pauk/`. Full architecture starts at
[`docs/architecture/overview.md`](docs/architecture/overview.md).

## Where to look

- **`docs/architecture/`** - how the system works today, one file per
  subpackage/stage. Start with `overview.md`; `scripts.md` indexes everything
  in `scripts/` and says which of them write to a database.
- **`docs/diagrams/`** - schemas as markdown+Mermaid, not images. Don't
  create PNG/SVG for new diagrams: text Mermaid is easier to search, review
  and edit, and GitHub renders it natively.
- **`README.md`** - quick start and a short DB schema for someone who
  hasn't read anything else.

Before an architectural change, check `docs/architecture/` first, don't
start from scratch. If a decision contradicts what's written there - either
you're wrong or the docs are stale; either way, sort that out before
changing code.

The same applies before running anything on the lab server, and before
reasoning about which rows a stage can see: `deploy.md` names the host, the
account and the shell the server needs; `storage.md` explains that in
`PreparedStore`, `read_rows` and `read_models` filter by group, while
`get_rows` and `get_models` look up specific IDs across groups. Both are
routinely guessed at from the environment and both are routinely guessed
wrong, at the cost of a wasted run or a confident wrong conclusion. Read them.

## Repository rules

- **New unit and integration tests use `unittest.TestCase`, not pytest
  style** (fixtures, `@pytest.mark`, bare `assert`). Benchmark tests in
  `tests/bench/` are an exception and may use pytest fixtures and style.
  CI runs tests through `uv run --with pytest pytest tests/ -q` (pytest as
  a runner can execute unittest tests).
  Run locally: `uv run python -m unittest discover -s tests/unit`.
  CI's `tests/` run includes `tests/bench/`. CI also runs integration tests
  in a separate step with Docker, `pytest`, `mongomock`, and `testcontainers`:
  `uv run --with pytest --with mongomock --with 'testcontainers[neo4j]'
  pytest tests/integration -q`.
- **Linter is `ruff`**, config in `pyproject.toml`. Before calling a change
  done: `uv run ruff check <changed files>`.
- **Code comments in English.**
- **Prefer docstrings to inline comments.** Avoid unnecessary inline
  comments; when needed, explain "why", not "what". Keep required tool
  directives. Comments don't attribute a person or PR number - that belongs
  in the commit message, not the code.
- **Commit messages follow Conventional Commits**: `type(scope): short
  description`, type by meaning (`feat`/`fix`/`refactor`/`chore`/`docs`) -
  see `git log` for this repo's actual style.
- **Don't rename Neo4j relationships/labels without strong reason.**
  `pauk/cache/export.py` finds data by exact relationship names - a
  silent mismatch won't break with an error, `MATCH` will just stop
  finding anything.
- **Neo4j properties don't store nested map/list-of-map values, or `null`
  inside an array.** Nested structures serialize to JSON text
  (`extract.py::JSON_TEXT_FIELDS`); `null` in an array property is
  replaced with a sentinel at the graph boundary (example -
  `page_number=0` instead of `None` for the abstract, see
  `docs/architecture/pipeline/code-links.md`).
- **Git operations follow the global rules.** Local operations, including
  fetch and pull, are allowed; warn the user before local operations that
  change state and preserve their changes. Read-only checks need no separate
  approval. Changes to GitHub or another cloud repository require explicit
  permission, as do publishing and irreversible actions.

## Before saying "done"

1. `uv run python -m unittest discover -s tests/unit` - green.
2. `uv run ruff check <changed files>` - clean.
3. If something `pauk/cache/export.py` reads changed (graph node,
   relationship, or property names) - check against
   `docs/architecture/neo4j-graph.md`, don't rely on memory.
4. Real check on live data if extraction/parsing changed - a synthetic
   test on constructed data systematically misses what only shows up on
   real data.
