# Local MCP prototype

`pauk/mcp/` is an optional, read-only Streamable HTTP interface to an
**isolated test graph**. It is not deployed, does not run an LLM and does not
integrate with a working CoScientist instance. Install only in the prototype's
own environment:

```bash
uv sync --extra mcp
uv run --no-sync pauk-mcp
```

The console entry requires explicit `PAUK_MCP_TOKEN`, `PAUK_MCP_NEO4J_URI`,
`PAUK_MCP_NEO4J_USER`, `PAUK_MCP_NEO4J_PASSWORD`, `PAUK_MCP_NEO4J_DATABASE` and
`PAUK_MCP_DATASET_POLICY`. These do not fall back to the pipeline's graph
settings. The URI must be `bolt://127.0.0.1:<non-default-port>` or
`bolt://localhost:<non-default-port>` without embedded credentials or routing
parameters. The default Bolt port is rejected to avoid accidentally selecting
an existing local graph. This is an accident-prevention check, not proof of a
database's identity or its permissions.

Policy `isolated` withholds description, context and classifier reason. Policy
`safe_synthetic` discloses those fields and is only for an explicitly approved
safe/synthetic dataset. Neither policy changes database privileges. The graph
must be isolated by its operator. `READ_ACCESS` is a routing hint, **not** an
enforced read-only database role. There is no production access-control claim.

The server binds `127.0.0.1`, on `PAUK_MCP_PORT` (default 8091). Every HTTP
request needs `Authorization: Bearer <PAUK_MCP_TOKEN>`; the token must contain
at least 32 non-whitespace ASCII characters. Comparison is constant-time and
happens before reading a request body or calling the graph. This is machine
Bearer authentication, not OAuth discovery. The SDK additionally validates
Host and Origin against loopback names. The stateless JSON transport does not
provide a GET/SSE notification channel.

`create_app(..., lifespan=callback)` runs that callback once for the outer
Starlette application, receiving that application as its argument. It wraps
and preserves the SDK session-manager lifespan; shared resources close after
SDK shutdown, not after individual stateless MCP requests.

## Public tools

All inputs are flat parameters; the tools publish structured output schemas.
Every root response identifies `source_system="PAUK"` and
`retrieval_mode="cached_prepared_graph"`. These are output-only fields: the
calls read the prepared PAUK graph, not live Semantic Scholar, Crossref or
repository APIs. They do not imply a current snapshot/access-check timestamp.

- `search_publications(doi=None, title_contains=None, repository_contains=None, page_size=10, cursor=None)`:
  exactly one search mode. DOI equality removes a DOI URL/`doi:` wrapper and
  ignores case. Title search matches publications whose title contains every
  whitespace-separated token using literal, case-insensitive `CONTAINS` (not
  regex), ordering exact-phrase matches and `IMPLEMENTS`-linked publications
  first. Repository search matches publications linked via
  `(:Repository)-[:IMPLEMENTS]->(:Publication)` (never `MENTIONS_LINK`) whose
  public repository `name` and approved-host `url` together contain every
  whitespace-separated token using literal, case-insensitive `CONTAINS`,
  ordering exact repository name matches and exact phrase matches first. Returns
  canonical publication ID, title, DOI and year.
- `get_publication_repositories(publication_id, page_size=10, cursor=None)`:
  only `(:Repository)-[:IMPLEMENTS]->(:Publication)` links. A mention alone is
  not author code. Empty means unknown in PAUK, not no code anywhere.
- `get_repository(repository_id)`: projected URL, name, description, language,
  license, archived and upstream `last_updated`; missing values remain unknown.
- `explain_link(publication_id, repository_id, page_size=5, cursor=None)`:
  reports `implements` separately from actual `MENTIONS_LINK` evidence,
  including rejected and unclassified mentions. Two existing objects without
  a relation return `false` and empty evidence.

Repository `metadata_note` and `verification_note` describe this call's limits:
`last_updated` is a cached upstream update date, not an access check or proof
of current maintenance/abandonment. `language` is an upstream label, not a code
inspection result; Jupyter Notebook does not by itself establish Python.
This call does not check availability, inspect code or test completeness or
reproducibility. Cache/access-check timestamps are not reported.

An existing direct ID wins over `merged_ids`. Missing IDs return `NOT_FOUND`;
multiple alias survivors return `AMBIGUOUS_ID`. Other public tool errors are
`INPUT`, `CURSOR`, `TIMEOUT`, `BACKEND` and `INTERNAL`, never exception text.

Free-text fields use `{value, status, truncated}`. `withheld` differs from
`unavailable`; neither can carry a value. Text is capped at 1,000 characters,
reason at 500, IDs at 256, DOI at 512, title queries at 3-300, URL at 2,000.
IDs and URLs are never shortened. Unsupported, credential-bearing, query-bearing,
fragment-bearing or unapproved-host URLs are withheld as null. Approved hosts
are explicitly listed in `contracts.safe_url`; this is not DNS-based safety
checking, and the server never fetches a returned URL.

Publication/repository pages default to 10, maximum 25; evidence defaults to
5, maximum 10. Cursors are HMAC-authenticated JSON continuations bound to the
normalized request and page size, ordered by canonical ID or graph edge ID
plus occurrence position. They expire on server restart because the signing
key is ephemeral. Pagination is not a snapshot: graph mutation between calls
can shift offsets.

## Evidence limits

Each evidence entry has `unit="extraction_occurrence"`. The root
`evidence_note` scopes these entries to this publication-repository pair;
multiple occurrences/source kinds are not independent sources. Document count
and unique file/version identity remain unknown. Stored `pdf_id` values are
logical cached references (often a publication ID), not file hashes, versions
or URLs; they are not exposed or used to invent grouping/source counts.

The current graph writer stores parallel native arrays: `context`,
`page_number`, `pdf_id`, with optional `context_source`. The query validates
core array types and lengths. Optional source arrays must match exactly;
absent, short, long or malformed source arrays make every source unknown.
It returns explicit unavailable evidence for scalar legacy or
misaligned core data. It never silently zips/truncates incompatible arrays.
`candidate_urls` is a union, not aligned provenance, and is not exposed.

Page zero alone is ambiguous: the writer also uses it for missing pages.
Only explicit `context_source='abstract'` permits an abstract/page-zero claim.
Positive page labels do not establish a unique file/version; this path does not persist
the chosen PDF's source URL, so `source_url` is always null rather than guessed
from `pdf_id` or a publication's candidate PDFs. No file identifiers or local
paths are returned. A finite numeric classifier score in [0, 1] is retained
without calibration claims; missing, malformed, boolean, infinite and
out-of-range scores become unknown, not zero and not a clamped probability.

## Bounds and validation

The reader uses fixed parameterized projections, no all-properties reads,
Person objects, schema initialization or mutation/admin imports. Async Neo4j
queries have a 3-second transaction and coroutine deadline, no retries; a
5-second tool deadline propagates cancellation and cancels the active session.
The HTTP boundary limits request bodies to 8 KiB and complete response bodies
(including SDK text plus structured-result framing) to 64 KiB. An oversized
response becomes a small valid HTTP 500 JSON error; it is never byte-truncated.
Clients should retry a legitimate large page with a smaller page size.

```bash
uv run --no-sync python -m unittest tests.unit.test_mcp_service tests.unit.test_mcp_server
PAUK_MCP_INTEGRATION=1 uv run --no-sync python -m unittest tests.integration.test_mcp_graph
```

The first command uses synthetic in-memory records and a real loopback HTTP
server/client. Without the optional SDK, only protocol tests are skipped; base
pipeline imports remain independent. The second command requires an already
cached `neo4j:2026.05.0` Docker image, creates one new named disposable container
with synthetic credentials and a random loopback Bolt port, seeds synthetic
records and stops only that owned container. It never pulls images, connects
to an existing graph or mounts user storage. Ordinary test discovery does not
connect to Neo4j.

For a separate client's local contract check, `tests.mcp_support` exports
`SyntheticReader`, `TEST_TOKEN` and `live_server(app)`. Build the app with
`create_app(Service(SyntheticReader(), b'synthetic-key'), TEST_TOKEN)`.
Fixture IDs are `p-paper`, `r-code`, `r-dependency`; `old-paper` resolves to
`p-paper`, and `ambiguous-repo` is deliberately ambiguous. The fixture DOI is
`10.1234/pauk-test`. This proves the local protocol contract only, not agent
reasoning quality, production permissions or deployed-client compatibility.

The separate [ADK compatibility gate](../../tests/integration/adk/README.md)
reproduces 24 synthetic fixture cases through the actual ADK client in its own
environment. It does not exercise a complete CoScientist workflow, models or
production. That README is authoritative for client setup and commands.

## CoScientist registration

In the separate CoScientist environment, set model/API environment variables
(`LLM__MAIN_MODEL`, `LLM__OPENAI_API_KEY`, `OPENROUTER_API_KEY`) before the
first `CoScientist` import (because `CoScientist.agents.common` binds the model
and LiteLLM API key at import time), and register `pauk` before
`manager.initialize`: appending tools afterwards leaves the native unknown-tool
guard stale. Add `pauk` to the existing `agents.ResearchAgent.tools` YAML list,
keeping other tools; select that profile with `COSCIENTIST_CONFIG` before
initialization. Because CoScientist eagerly builds at package import, first
import it with the existing/default profile, then register `pauk`, then select
the PAUK-enabled `COSCIENTIST_CONFIG`, and only then call `manager.initialize`.

```python
import os
from CoScientist.assembly.registry import REGISTRY, ToolDoc, ToolEntry
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset


def make_pauk_toolset() -> McpToolset:
    """Connect to the explicitly configured local PAUK prototype."""
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=os.environ["PAUK_MCP_URL"],
            headers={"Authorization": "Bearer " + os.environ["PAUK_MCP_TOKEN"]},
        ),
        tool_filter=["search_publications", "get_publication_repositories",
                     "get_repository", "explain_link"],
    )


REGISTRY.register_tool(ToolEntry(
    key="pauk", factory=make_pauk_toolset, runtime_resolved=True,
    docs=(
        ToolDoc("search_publications", "search_publications(doi=None, title_contains=None, repository_contains=None, page_size=10, cursor=None)",
                "Find publications by exactly one DOI, title fragment, or confirmed repository name/URL."),
        ToolDoc("get_publication_repositories",
                "get_publication_repositories(publication_id, page_size=10, cursor=None)",
                "Get stored IMPLEMENTS author-code links; empty means unknown in PAUK."),
        ToolDoc("get_repository", "get_repository(repository_id)", "Get cached repository metadata."),
        ToolDoc("explain_link", "explain_link(publication_id, repository_id, page_size=5, cursor=None)",
                "Separate IMPLEMENTS from stored mentions without inferred PDF provenance."),
    ),
))
```

Set client-side `PAUK_MCP_URL` (for example `http://127.0.0.1:8091/mcp`) and
`PAUK_MCP_TOKEN` through the environment, not YAML. This registration pattern was
checked with CoScientist `c64f579` (ADK `2.3.0`, MCP `1.28.0`) and `900aeae2`
(ADK `2.12.0`, MCP `1.30.0`), not other revisions or production deployments.
CoScientist and ADK are not PAUK runtime dependencies.
