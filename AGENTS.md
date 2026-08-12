# AGENTS.md

Instructions for an agent, and for a human opening this repository for the
first time. Rules and pointers live here; how the system actually works
lives in `docs/`, not duplicated here.

## What this project is

PAUK collects ITMO's publications from OpenAlex, enriches them (Crossref,
ORCID, OpenReview, GitHub), finds code links in them, and loads the result
into Neo4j. `pauk/gui/` renders an interactive map of that graph.

One Python package, `pauk/`. Full architecture starts at
[`docs/architecture/overview.md`](docs/architecture/overview.md).

## Where to look

- **`docs/architecture/`** - how the system works today, one file per
  subpackage/stage. Start with `overview.md`.
- **`docs/diagrams/`** - schemas as markdown+Mermaid, not images. Don't
  create PNG/SVG for new diagrams - models can't read them, and text
  Mermaid renders natively on GitHub and reads like plain text.
- **`README.md`** - quick start and a short DB schema for someone who
  hasn't read anything else.

Before an architectural change, check `docs/architecture/` first, don't
start from scratch. If a decision contradicts what's written there - either
you're wrong or the docs are stale; either way, sort that out before
changing code.

## Repository rules

- **Tests use `unittest.TestCase`, not pytest style** (fixtures,
  `@pytest.mark`, bare `assert`). CI runs them through
  `uv run --with pytest pytest tests/ -q` (pytest as a runner can execute
  unittest tests), but they're always written as `unittest.TestCase`.
  Run locally: `uv run python -m unittest discover -s tests/unit`.
  `tests/bench/` is separate, needs `pytest` in the environment, and isn't
  part of the normal run. `tests/integration/` is also separate: needs
  Docker and `testcontainers` (neither is a project dependency), skips
  itself cleanly via `setUpModule` when either is missing so the plain CI
  command above still passes untouched. Run explicitly: `uv run --with
  pytest --with 'testcontainers[neo4j]' python -m pytest tests/integration
  -q`.
- **Linter is `ruff`**, config in `pyproject.toml`. Before calling a change
  done: `uv run ruff check <changed files>`.
- **Code comments in English.**
- **A comment explains "why", not "what".** If the code already makes
  clear what a line does, skip the comment. Comments don't attribute a
  person or PR number - that belongs in the commit message, not the code.
- **Commit messages follow Conventional Commits**: `type(scope): short
  description`, type by meaning (`feat`/`fix`/`refactor`/`chore`/`docs`) -
  see `git log` for this repo's actual style.
- **Don't rename Neo4j relationships/labels without strong reason.**
  `pauk/gui/generate_data.py` finds data by exact relationship names - a
  silent mismatch won't break with an error, `MATCH` will just stop
  finding anything.
- **Neo4j properties don't store nested map/list-of-map values, or `null`
  inside an array.** Nested structures serialize to JSON text
  (`extract.py::JSON_TEXT_FIELDS`); `null` in an array property is
  replaced with a sentinel at the graph boundary (example -
  `page_number=0` instead of `None` for the abstract, see
  `docs/architecture/pipeline/code-links.md`).
- **No git operations without explicit user approval.** Editing files and
  running tests in the working tree is free to do; `git commit`, `push`
  (especially `--force`), creating/switching branches, and merging a PR
  each need an explicit go-ahead from the user for that specific action -
  approval for one doesn't carry over to the next. A branch whose history
  is already published isn't rewritten without a warning about the
  consequences.

## Before saying "done"

1. `uv run python -m unittest discover -s tests/unit` - green.
2. `uv run ruff check <changed files>` - clean.
3. If something `pauk/gui/generate_data.py` reads changed (graph node,
   relationship, or property names) - check against
   `docs/architecture/neo4j-graph.md`, don't rely on memory.
4. Real check on live data if extraction/parsing changed - a synthetic
   test on constructed data systematically misses what only shows up on
   real data.
