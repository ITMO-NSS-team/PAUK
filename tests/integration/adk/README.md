# ADK MCP compatibility gate

Opt-in check of the real PAUK Streamable HTTP app with the Google ADK client
pattern used by CoSci. Client targets: (**google-adk 2.3.0**, **mcp 1.28.0**) or
(**google-adk 2.12.0**, **mcp 1.30.0**), Python 3.12. The client checks these
installed version pairs before testing.
`requirements.txt` pins direct dependencies only; it is **not a transitive lock**.
The original Windows run used Python 3.12.13 (and Python 3.12.11 for the
`CoScientist_dev` 2.12.0/1.30.0 environment). Other platforms and newly resolved
transitive dependency sets need their own run; no cross-platform claim is made.

## Separate environments

Run `run.py` with PAUK's existing Python environment, which supplies the real
server and `tests/mcp_support.py`. Install the client requirements in a separate
environment outside the repository. Do not add ADK to PAUK runtime dependencies.
No full CoResearchGraph or FEDOT.MAS installation is needed.

From the repository root, Git Bash on Windows:

```bash
CLIENT_ENV=/absolute/path/to/separate-adk-client
uv venv --python .venv/Scripts/python.exe --no-python-downloads "$CLIENT_ENV"
uv pip install --python "$CLIENT_ENV/Scripts/python.exe" \
  -r tests/integration/adk/requirements.txt
.venv/Scripts/python.exe tests/integration/adk/run.py \
  --client-python "$CLIENT_ENV/Scripts/python.exe"
```

For POSIX environments, use `.venv/bin/python` and `$CLIENT_ENV/bin/python`
instead. Reuse an already prepared client environment by skipping installation.
No Python, container image, model, dataset, or model weights are downloaded by
the runner. Dependency installation is a separate, explicit step.

## What runs

`run.py` starts the actual `create_app(Service(SyntheticReader(), ...))` through
existing `live_server`, on an ephemeral loopback port. It generates a fresh
synthetic machine token and cursor key. No Neo4j instance or production settings
are required. Only this temporary server is stopped on exit. It launches
`client.py` with the explicitly supplied client interpreter and a minimal
inherited environment; the token travels only through `PAUK_MCP_TEST_TOKEN`.
No token is accepted as a CLI argument or printed. The runner writes no reports,
logs, fixture copies, or environments into the repository.

The client constructs a real `ToolContext` from `InvocationContext` and
`InMemorySessionService`, leaving the agent unset. There is no Runner, LLM,
model call, ADC probe, login, or API key. Before SDK imports it sets
`GOOGLE_API_USE_CLIENT_CERTIFICATE=false`. Tool discovery, keyword-only
`run_async(args=..., tool_context=...)` calls and `close()` share an async task.
Assertions are acceptance checks, so optimized Python execution is refused.

Checks include:

- All six PAUK tools and `tool_filter` discovery of `search_publications`.
- All 36 synthetic cases in `cases.json`: DOI/title/publication-repository search,
  DOI normalization, repository search by URL/query, reverse publication lookup
  (`implements` and `mentions`), canonical aliases, repository metadata,
  accepted author-code links, and rejected dependency mentions that must not
  become author-code links.
- Empty search, unknown/ambiguous identifiers, missing/extra/malformed arguments,
  strict page-size types, evidence bounds and tampered cursors.
- The full MCP envelope (`content`, `structuredContent`, `isError`), recursive
  expected subsets and JSON-Pointer fixture IDs; not just `structuredContent`.
- Missing/wrong machine-token rejection and successful authenticated calls.
- Token reflection and common internal-error detail rejection. This is not an
  exhaustive privacy audit. Synthetic free text is explicitly enabled; the
  separate server tests own the default private-text withholding policy.
- Refusal of external URLs, redirects and environment proxies; time-bounded
  discovery/calls/cleanup and an unreachable reserved loopback port.

The outcome is one redacted JSON summary and a nonzero exit status on failure.
Child output is captured and suppressed on failure to avoid exposing credentials
or server payloads. SDK/server warnings may appear without fixture contents.
Scripts are intentionally not named `test_*.py`, so ordinary unittest discovery
does not import client-only dependencies.

## Scope of evidence

A passing run demonstrates this concrete client library pair against PAUK's real
server implementation using the existing synthetic reader. It does not test the
full CoSci workflow, agent/model execution, production Neo4j data, deployment,
OAuth, or other SDK versions. Running these scripts does not change CoSci or
PAUK server code. Keep broader server/security tests as separate checks.
