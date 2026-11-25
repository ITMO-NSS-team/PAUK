"""Fixed, parameterized, bounded MATCH queries; no graph mutation imports."""

import asyncio
from typing import Any, Literal, Protocol

from neo4j import READ_ACCESS, AsyncDriver, Query
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from pauk.mcp.contracts import APPROVED_REPO_HOSTS, MCPError

NODE_ID = "CASE WHEN n.id IS NOT NULL AND n.id IS :: STRING AND size(n.id) <= 256 THEN n.id ELSE null END AS id"
PUBLICATION = (
    NODE_ID
    + ", CASE WHEN n.title IS NOT NULL AND n.title IS :: STRING THEN substring(n.title, 0, 1001) ELSE null END AS title, "
    "CASE WHEN n.doi IS NOT NULL AND n.doi IS :: STRING AND size(n.doi) <= 512 THEN n.doi ELSE null END AS doi, "
    "n.year AS year"
)
REPOSITORY_LINK = (
    "CASE WHEN r.id IS NOT NULL AND r.id IS :: STRING AND size(r.id) <= 256 THEN r.id ELSE null END AS id, "
    "CASE WHEN r.url IS NOT NULL AND r.url IS :: STRING AND size(r.url) <= 2000 THEN r.url ELSE null END AS url, "
    "CASE WHEN r.name IS NOT NULL AND r.name IS :: STRING THEN substring(r.name, 0, 1001) ELSE null END AS name"
)
RESOLVE = {
    "Publication": (
        "MATCH (n:Publication) WHERE n.id = $id RETURN " + NODE_ID + " LIMIT 2",
        "MATCH (n:Publication) WHERE $id IN coalesce(n.merged_ids, []) RETURN " + NODE_ID + " ORDER BY id LIMIT 2",
    ),
    "Repository": (
        "MATCH (n:Repository) WHERE n.id = $id RETURN " + NODE_ID + " LIMIT 2",
        "MATCH (n:Repository) WHERE $id IN coalesce(n.merged_ids, []) RETURN " + NODE_ID + " ORDER BY id LIMIT 2",
    ),
}
DOI_VALUE = "toLower(trim(coalesce(n.doi, '')))"
NORMALIZED_DOI = (
    "CASE WHEN d STARTS WITH 'https://doi.org/' THEN trim(substring(d, 16)) "
    "WHEN d STARTS WITH 'http://doi.org/' THEN trim(substring(d, 15)) "
    "WHEN d STARTS WITH 'https://dx.doi.org/' THEN trim(substring(d, 19)) "
    "WHEN d STARTS WITH 'http://dx.doi.org/' THEN trim(substring(d, 18)) "
    "WHEN d STARTS WITH 'doi:' THEN trim(substring(d, 4)) ELSE d END"
)
SEARCH_DOI = (
    f"MATCH (n:Publication) WITH n, {DOI_VALUE} AS d WHERE {NORMALIZED_DOI} = $query "
    f"RETURN {PUBLICATION} ORDER BY id SKIP $offset LIMIT $limit"
)
_APPROVED_REPO_ORIGIN_MAP = [
    [f"{scheme}://{prefix}{host}{port}", host]
    for host in sorted(APPROVED_REPO_HOSTS)
    for prefix in ("", "www.")
    for scheme, port in (("https", ""), ("https", ":443"), ("http", ""), ("http", ":80"))
]
_APPROVED_REPO_ORIGINS = [origin for origin, _ in _APPROVED_REPO_ORIGIN_MAP]
SAFE_REPO_URL = (
    "CASE WHEN r.url IS NOT NULL AND r.url IS :: STRING AND size(r.url) <= 2000 "
    "AND NOT (r.url CONTAINS '@' OR r.url CONTAINS '?' OR r.url CONTAINS '#' "
    "OR r.url CONTAINS ' ' OR r.url CONTAINS '\\t' OR r.url CONTAINS '\\n' OR r.url CONTAINS '\\r') "
    f"AND ANY(origin IN {_APPROVED_REPO_ORIGINS!r} "
    "WHERE toLower(r.url) = origin OR toLower(r.url) STARTS WITH origin + '/') "
    "THEN toLower(r.url) ELSE '' END"
)
SEARCH_TITLE = (
    "MATCH (n:Publication) WITH n, toLower(coalesce(n.title, '')) AS title "
    "WHERE ALL(token IN split($query, ' ') WHERE token = '' OR title CONTAINS token) "
    "WITH n, title CONTAINS $query AS exact_phrase, "
    "EXISTS { (:Repository)-[:IMPLEMENTS]->(n) } AS has_implements "
    f"RETURN {PUBLICATION} ORDER BY exact_phrase DESC, has_implements DESC, id ASC SKIP $offset LIMIT $limit"
)
SEARCH_REPOSITORY = (
    "MATCH (r:Repository)-[:IMPLEMENTS]->(n:Publication) "
    "WITH n, CASE WHEN r.name IS NOT NULL AND r.name IS :: STRING THEN toLower(r.name) ELSE '' END AS repo_name, "
    f"({SAFE_REPO_URL}) AS repo_url "
    "WITH n, repo_name, repo_name + ' ' + repo_url AS repo_text "
    "WHERE ALL(token IN split($query, ' ') WHERE token = '' OR repo_text CONTAINS token) "
    "WITH n, "
    "max(CASE WHEN repo_name = $query THEN 1 ELSE 0 END) AS exact_name, "
    "max(CASE WHEN repo_text CONTAINS $query THEN 1 ELSE 0 END) AS exact_phrase "
    "ORDER BY exact_name DESC, exact_phrase DESC, n.id ASC "
    "SKIP $offset LIMIT $limit "
    f"RETURN {PUBLICATION}"
)
REPOSITORIES = (
    "MATCH (r:Repository)-[:IMPLEMENTS]->(p:Publication {id: $publication_id}) "
    f"RETURN DISTINCT {REPOSITORY_LINK} ORDER BY id SKIP $offset LIMIT $limit"
)
REPOSITORY = (
    "MATCH (r:Repository {id: $repository_id}) RETURN " + REPOSITORY_LINK + ", "
    "CASE WHEN $text AND r.description IS NOT NULL AND r.description IS :: STRING "
    "THEN substring(r.description, 0, 1001) ELSE null END AS description, "
    "r.description IS NOT NULL AS description_present, "
    "CASE WHEN r.language IS NOT NULL AND r.language IS :: STRING THEN substring(r.language, 0, 1001) ELSE null END AS language, "
    "CASE WHEN r.license IS NOT NULL AND r.license IS :: STRING THEN substring(r.license, 0, 1001) ELSE null END AS license, "
    "r.archived AS archived, toString(r.last_updated) AS last_updated LIMIT 2"
)
SEARCH_REPOSITORIES_URL = (
    f"MATCH (r:Repository) WITH r, ({SAFE_REPO_URL}) AS safe_url "
    "WHERE safe_url <> '' "
    f"WITH r, safe_url, head([pair IN {_APPROVED_REPO_ORIGIN_MAP!r} "
    "WHERE safe_url = pair[0] OR safe_url STARTS WITH pair[0] + '/']) AS origin_pair "
    "WHERE origin_pair IS NOT NULL "
    "WITH r, origin_pair[1] AS canonical_host, substring(safe_url, size(origin_pair[0])) AS raw_path "
    "WITH r, canonical_host, "
    "left(raw_path, reduce(end_idx = 0, idx IN range(0, size(raw_path) - 1) | "
    "CASE WHEN substring(raw_path, idx, 1) <> '/' THEN idx + 1 ELSE end_idx END)) AS s1 "
    "WITH r, canonical_host, CASE WHEN s1 ENDS WITH '.git' THEN left(s1, size(s1) - 4) ELSE s1 END AS s2 "
    "WITH r, canonical_host, "
    "left(s2, reduce(end_idx = 0, idx IN range(0, size(s2) - 1) | "
    "CASE WHEN substring(s2, idx, 1) <> '/' THEN idx + 1 ELSE end_idx END)) AS clean_path "
    "WHERE clean_path <> '' AND ('https://' + canonical_host + clean_path) = $query "
    f"RETURN {REPOSITORY_LINK} ORDER BY id ASC SKIP $offset LIMIT $limit"
)
SEARCH_REPOSITORIES_QUERY = (
    "MATCH (r:Repository) "
    "WITH r, CASE WHEN r.name IS NOT NULL AND r.name IS :: STRING THEN toLower(r.name) ELSE '' END AS repo_name, "
    f"({SAFE_REPO_URL}) AS repo_url "
    "WITH r, repo_name, repo_name + ' ' + repo_url AS repo_text "
    "WHERE ALL(token IN split($query, ' ') WHERE token = '' OR repo_text CONTAINS token) "
    "WITH r, CASE WHEN repo_name = $query THEN 1 ELSE 0 END AS exact_name, "
    "CASE WHEN repo_text CONTAINS $query THEN 1 ELSE 0 END AS exact_phrase, "
    "EXISTS { (r)-[:IMPLEMENTS]->(:Publication) } AS has_implements "
    f"RETURN {REPOSITORY_LINK} ORDER BY exact_name DESC, exact_phrase DESC, has_implements DESC, id ASC "
    "SKIP $offset LIMIT $limit"
)
REPOSITORY_IMPLEMENTS_PUBLICATIONS = (
    "MATCH (r:Repository {id: $repository_id})-[:IMPLEMENTS]->(n:Publication) "
    f"RETURN DISTINCT {PUBLICATION}, true AS implements, null AS relevant "
    "ORDER BY id ASC SKIP $offset LIMIT $limit"
)
REPOSITORY_MENTIONS_PUBLICATIONS = (
    "MATCH (n:Publication)-[m:MENTIONS_LINK]->(r:Repository {id: $repository_id}) "
    "WITH n, EXISTS { (r)-[:IMPLEMENTS]->(n) } AS implements, collect(m.is_relevant) AS verdicts "
    "WITH n, implements, "
    "CASE WHEN ANY(v IN verdicts WHERE v = true) THEN true "
    "WHEN ANY(v IN verdicts WHERE v = false) THEN false ELSE null END AS relevant "
    f"RETURN {PUBLICATION}, implements, relevant "
    "ORDER BY implements DESC, id ASC SKIP $offset LIMIT $limit"
)
IMPLEMENTS = (
    "MATCH (r:Repository {id: $repository_id})-[:IMPLEMENTS]->(p:Publication {id: $publication_id}) "
    "RETURN true AS implements LIMIT 1"
)
EVIDENCE = """
MATCH (p:Publication {id: $publication_id})-[m:MENTIONS_LINK]->(r:Repository {id: $repository_id})
WITH m,
     CASE WHEN m.context IS :: LIST<STRING> THEN m.context ELSE [] END AS contexts,
     CASE WHEN m.page_number IS :: LIST<INTEGER> THEN m.page_number ELSE [] END AS pages,
     CASE WHEN m.pdf_id IS :: LIST<STRING> THEN m.pdf_id ELSE [] END AS pdfs,
     CASE WHEN m.context_source IS :: LIST<STRING> THEN m.context_source ELSE [] END AS sources
WITH m, contexts, pages, sources, size(contexts) AS count,
     size(contexts) > 0 AND size(contexts) = size(pages) AND size(contexts) = size(pdfs) AS aligned
UNWIND CASE WHEN aligned THEN range(0, count - 1) ELSE [0] END AS occurrence
RETURN elementId(m) AS edge, occurrence, aligned,
       CASE WHEN $text AND aligned THEN substring(contexts[occurrence], 0, 1001) ELSE null END AS context,
       aligned AND coalesce(contexts[occurrence], '') <> '' AS context_present,
       CASE WHEN aligned THEN pages[occurrence] ELSE null END AS page,
       CASE WHEN aligned AND size(sources) = count THEN sources[occurrence] ELSE null END AS source,
       m.is_relevant AS relevant, m.llm_confidence AS confidence,
       CASE WHEN $text THEN substring(m.llm_reason, 0, 501) ELSE null END AS reason,
       m.llm_reason IS NOT NULL AS reason_present,
       CASE WHEN m.classification_status IN ['pending', 'classified', 'failed']
            THEN m.classification_status ELSE null END AS classification_status
ORDER BY edge, occurrence SKIP $offset LIMIT $limit
"""


class Reader(Protocol):
    """Minimal injectable graph interface."""

    async def query(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
        """Execute one fixed bounded query."""
        ...


class Neo4jReader:
    """Execute cancellable async queries with no transaction retry."""

    def __init__(self, driver: AsyncDriver, database: str, query_timeout: float = 3.0) -> None:
        self.driver = driver
        self.database = database
        self.query_timeout = query_timeout

    async def query(self, query: str, parameters: dict[str, object]) -> list[dict[str, Any]]:
        """Bound server transaction time and cancel its session on task cancellation."""
        session = self.driver.session(database=self.database, default_access_mode=READ_ACCESS)
        try:
            async with session:
                try:
                    async with asyncio.timeout(self.query_timeout):
                        result = await session.run(Query(query, timeout=self.query_timeout), parameters)
                        return [record.data() async for record in result]
                except asyncio.CancelledError:
                    session.cancel()
                    raise
                except TimeoutError as exc:
                    session.cancel()
                    raise MCPError("TIMEOUT") from exc
        except MCPError:
            raise
        except (Neo4jError, ServiceUnavailable, SessionExpired, OSError) as exc:
            raise MCPError("BACKEND") from exc


async def resolve(reader: Reader, kind: Literal["Publication", "Repository"], identifier: str) -> str:
    """Prefer an existing canonical id, then require a unique merged-id match."""
    for query in RESOLVE[kind]:
        rows = await reader.query(query, {"id": identifier})
        if len(rows) > 1:
            raise MCPError("AMBIGUOUS_ID")
        if rows:
            return rows[0]["id"]
    raise MCPError("NOT_FOUND")
