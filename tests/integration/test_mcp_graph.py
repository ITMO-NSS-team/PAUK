"""Synthetic Neo4j checks in a disposable testcontainers instance, never an existing graph.

Neither Docker nor `testcontainers` is a project dependency (see AGENTS.md). Run explicitly, with Docker
running:

    uv run --extra mcp --with pytest --with 'testcontainers[neo4j]' pytest tests/integration/test_mcp_graph.py -q

Defaults to `neo4j:5-community` (shared with the other Neo4j integration tests); set
`PAUK_MCP_NEO4J_IMAGE` to test against another image (such as `neo4j:2026.05.0`). If `testcontainers` is
missing or Docker is unreachable, the test class is skipped instead of erroring.
"""

import asyncio
import os
import time
import unittest

from neo4j import AsyncGraphDatabase, GraphDatabase

from pauk.mcp.contracts import (
    ExplainRequest,
    MCPError,
    PublicationRequest,
    RepositoryPublicationsRequest,
    RepositoryRequest,
    SearchRepositoriesRequest,
    SearchRequest,
)
from pauk.mcp.reader import Neo4jReader
from pauk.mcp.service import Service

try:
    from testcontainers.community.neo4j import Neo4jContainer

    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only when the extra isn't installed
    Neo4jContainer = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR = exc

DEFAULT_NEO4J_IMAGE = "neo4j:5-community"
SYNTHETIC_PASSWORD = "pauk-mcp-synthetic-only-password"
SEED = """
CREATE (p:Publication {id:'p-paper', merged_ids:['old-paper'], title:'Synthetic PAUK paper',
                       doi:'https://doi.org/10.1234/PAUK-TEST', year:2026}),
       (p2:Publication {id:'p-other', title:'Literal .* test', doi:'10.1234/pauk-test-extra'}),
       (r:Repository {id:'r-code', merged_ids:['old-code', 'ambiguous'], url:'https://github.com/pauk-test/code',
                      name:'Synthetic code', description:'PRIVATE_CANARY', language:'Python', license:'MIT',
                      archived:false, last_updated:datetime('2026-01-02T00:00:00Z')}),
       (d:Repository {id:'r-dependency', merged_ids:['ambiguous'], name:'Dependency'}),
       (nameless:Repository {id:'r-url-only', url:'https://github.com/pauk-test/nameless-repo'}),
       (multi:Repository {id:'r-multi', merged_ids:['old-multi'], url:'https://github.com/pauk-test/multi',
                          name:'Multi framework'}),
       (messy:Repository {id:'r-messy', url:'http://www.github.com:80/PAUK-TEST/messy.GIT///', name:42}),
       (doipath:Repository {id:'r-doi-port', url:'https://doi.org/10.1234/foo:80/bar', name:'DOI path port'}),
       (priv:Repository {id:'r-private', url:'https://private.example.org/secret', name:'PrivateHost'}),
       (pm1:Publication {id:'p-multi-1', title:'Framework paper one', year:2024}),
       (pm2:Publication {id:'p-multi-2', title:'Framework paper two', year:2025}),
       (pz:Publication {id:'p-z-impl', title:'Alphabetically late implemented', year:2026}),
       (pa:Publication {id:'p-a-reuse', title:'Alphabetically early reuse', year:2026}),
       (pn:Publication {id:'p-unknown-mention', title:'Unclassified mention', year:2026}),
       (pmix:Publication {id:'p-mixed-mention', title:'Mixed mention', year:2026}),
       (u:Repository {id:'r-unknown'}), (s:Repository {id:'r-scalar'}),
       (m:Repository {id:'r-misaligned'}), (n:Repository {id:'r-no-relation'}),
       (short:Repository {id:'r-short-source'}), (long:Repository {id:'r-long-source'}),
       (r)-[:IMPLEMENTS]->(p), (nameless)-[:IMPLEMENTS]->(p2),
       (multi)-[:IMPLEMENTS]->(pm1), (multi)-[:IMPLEMENTS]->(pm2), (multi)-[:IMPLEMENTS]->(pz),
       (pm1)-[:MENTIONS_LINK {context:['Author release'], page_number:[1], pdf_id:['m1'], is_relevant:true}]->(multi),
       (pz)-[:MENTIONS_LINK {context:['Late id implements'], page_number:[2], pdf_id:['m2'], is_relevant:true}]->(multi),
       (pa)-[:MENTIONS_LINK {context:['Early id reuse'], page_number:[3], pdf_id:['m3'], is_relevant:false}]->(multi),
       (pn)-[:MENTIONS_LINK {context:['Unclassified'], page_number:[4], pdf_id:['m4']}]->(multi),
       (pmix)-[:MENTIONS_LINK {context:['Rejected edge'], page_number:[1], pdf_id:['m5'], is_relevant:false}]->(multi),
       (pmix)-[:MENTIONS_LINK {context:['Unknown edge'], page_number:[2], pdf_id:['m6']}]->(multi),
       (p)-[:MENTIONS_LINK {context:['Synthetic abstract','Synthetic PDF'], page_number:[0,3], pdf_id:['','pdf-1'],
          context_source:['abstract','pdf_text'], candidate_urls:['https://github.com/unrelated/candidate'],
          is_relevant:true, classification_status:'classified', llm_confidence:0.0, llm_reason:'Synthetic reason'}]->(r),
       (p)-[:MENTIONS_LINK {context:['Dependency mention'], page_number:[2], pdf_id:['pdf-1'],
          is_relevant:false}]->(d),
       (p)-[:MENTIONS_LINK {context:['Unknown origin'], page_number:[0], pdf_id:['']}]->(u),
       (p)-[:MENTIONS_LINK {context:'Legacy scalar', page_number:3, is_relevant:true}]->(s),
       (p)-[:MENTIONS_LINK {context:['first','second'], page_number:[4], pdf_id:['a','b']}]->(m),
       (p)-[:MENTIONS_LINK {context:['first','second'], page_number:[0,0], pdf_id:['',''],
           context_source:['abstract'], classification_status:'PRIVATE_CANARY user@example.invalid /private/key'}]->(short),
       (p)-[:MENTIONS_LINK {context:['first','second'], page_number:[0,0], pdf_id:['',''],
           context_source:['abstract','abstract','abstract'], classification_status:42}]->(long)
"""


class GraphTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(
                f"testcontainers not installed ({_IMPORT_ERROR}); "
                "run with --with 'testcontainers[neo4j]' to include these tests"
            )
        image = os.getenv("PAUK_MCP_NEO4J_IMAGE", DEFAULT_NEO4J_IMAGE)
        try:
            container = Neo4jContainer(image=image, password=SYNTHETIC_PASSWORD)
            container.start()
        except Exception as exc:  # Docker daemon not running, no permission, image pull failure, etc.
            raise unittest.SkipTest(f"could not start a Neo4j container: {exc}") from exc
        cls._container = container
        cls.addClassCleanup(cls._container.stop)
        cls.uri = cls._container.get_connection_url()
        cls.username = cls._container.username
        with (
            GraphDatabase.driver(
                cls.uri, auth=(cls.username, SYNTHETIC_PASSWORD), connection_timeout=1, max_transaction_retry_time=0
            ) as driver,
            driver.session(database="neo4j") as session,
        ):
            session.run(SEED).consume()

    async def asyncSetUp(self) -> None:
        self.driver = AsyncGraphDatabase.driver(
            self.uri, auth=(self.username, SYNTHETIC_PASSWORD), max_transaction_retry_time=0
        )
        self.service = Service(Neo4jReader(self.driver, "neo4j"), b"synthetic", safe_synthetic_text=True)

    async def asyncTearDown(self) -> None:
        await self.driver.close()

    async def test_exact_doi_literal_title_canonical_aliases_and_metadata(self) -> None:
        page = await self.service.search_publications(SearchRequest(doi="DOI:10.1234/pauk-test"))
        self.assertEqual([p.publication_id for p in page.items], ["p-paper"])
        page = await self.service.search_publications(SearchRequest(title_contains=".* test"))
        self.assertEqual([p.publication_id for p in page.items], ["p-other"])
        page = await self.service.search_publications(SearchRequest(repository_contains="pauk-test/code"))
        self.assertEqual([p.publication_id for p in page.items], ["p-paper"])
        page = await self.service.search_publications(SearchRequest(repository_contains="pauk-test/nameless-repo"))
        self.assertEqual([p.publication_id for p in page.items], ["p-other"])
        page = await self.service.search_publications(SearchRequest(repository_contains="nameless-repo"))
        self.assertEqual([p.publication_id for p in page.items], ["p-other"])
        page = await self.service.search_publications(SearchRequest(repository_contains="Synthetic code"))
        self.assertEqual([p.publication_id for p in page.items], ["p-paper"])
        page = await self.service.search_publications(SearchRequest(repository_contains="Dependency"))
        self.assertEqual(page.items, [])
        repos = await self.service.get_publication_repositories(PublicationRequest(publication_id="old-paper"))
        self.assertEqual([r.repository_id for r in repos.items], ["r-code"])
        repo = await self.service.get_repository(RepositoryRequest(repository_id="old-code"))
        self.assertEqual(repo.repository_id, "r-code")
        self.assertIn("2026-01-02", repo.last_updated)
        self.assertEqual(repo.license.value, "MIT")
        self.assertFalse(repo.archived)
        with self.assertRaises(MCPError) as caught:
            await self.service.get_repository(RepositoryRequest(repository_id="ambiguous"))
        self.assertEqual(caught.exception.code, "AMBIGUOUS_ID")

    async def test_actual_parallel_arrays_sources_and_pagination(self) -> None:
        request = ExplainRequest(publication_id="p-paper", repository_id="r-code", page_size=1)
        first = await self.service.explain_link(request)
        self.assertTrue(first.implements)
        self.assertEqual(first.evidence[0].pages, [0])
        self.assertEqual(first.evidence[0].source_kind, "abstract")
        self.assertEqual(first.evidence[0].confidence, 0.0)
        self.assertIsNotNone(first.next_cursor)
        second = await self.service.explain_link(request.model_copy(update={"cursor": first.next_cursor}))
        self.assertEqual(second.evidence[0].pages, [3])
        self.assertEqual(second.evidence[0].source_kind, "pdf_text")
        self.assertIsNone(second.evidence[0].source_url)
        self.assertIsNone(second.next_cursor)

    async def test_rejected_unknown_scalar_and_misaligned_evidence(self) -> None:
        for identifier, verdict in (("r-dependency", "rejected"), ("r-unknown", "unknown")):
            result = await self.service.explain_link(
                ExplainRequest(publication_id="p-paper", repository_id=identifier)
            )
            self.assertFalse(result.implements)
            self.assertEqual(result.evidence[0].verdict, verdict)
            self.assertIsNone(result.evidence[0].confidence)
            self.assertIsNone(result.evidence[0].source_kind)
            if identifier == "r-unknown":
                self.assertEqual(result.evidence[0].pages, [])
        for identifier in ("r-scalar", "r-misaligned"):
            result = await self.service.explain_link(
                ExplainRequest(publication_id="p-paper", repository_id=identifier)
            )
            self.assertEqual(len(result.evidence), 1)
            self.assertEqual(result.evidence[0].status, "unavailable")
            self.assertIsNone(result.evidence[0].context.value)
        result = await self.service.explain_link(
            ExplainRequest(publication_id="p-paper", repository_id="r-no-relation")
        )
        self.assertFalse(result.implements)
        self.assertEqual(result.evidence, [])

    async def test_malformed_source_arrays_and_classification_status_are_unknown(self) -> None:
        for identifier in ("r-short-source", "r-long-source"):
            result = await self.service.explain_link(
                ExplainRequest(publication_id="p-paper", repository_id=identifier)
            )
            self.assertEqual(len(result.evidence), 2)
            for evidence in result.evidence:
                self.assertEqual(evidence.pages, [])
                self.assertIsNone(evidence.source_kind)
                self.assertEqual(evidence.classification_status.status, "unavailable")
            self.assertNotIn("PRIVATE_CANARY", result.model_dump_json())

    async def _probe_transactions(self, marker: str) -> list[dict[str, object]]:
        async with self.driver.session(database="neo4j") as session:
            result = await session.run(
                "SHOW TRANSACTIONS YIELD currentQuery WHERE currentQuery CONTAINS $marker RETURN currentQuery",
                {"marker": marker},
            )
            return [record.data() async for record in result]

    async def _run_cancelled_probe(self, query: str, marker: str) -> None:
        reader = Neo4jReader(self.driver, "neo4j", query_timeout=0.5)
        task = asyncio.create_task(reader.query(query, {}))
        observed = False
        try:
            for _ in range(30):
                if await self._probe_transactions(marker):
                    observed = True
                    break
                if task.done():
                    break
                await asyncio.sleep(0.01)
            with self.assertRaises(MCPError) as caught:
                await task
            self.assertIn(caught.exception.code, ("TIMEOUT", "BACKEND"))
            self.assertTrue(observed, "Probe never observed the expensive transaction running")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_query_deadline_cancels_real_server_transaction(self) -> None:
        marker = "pauk-mcp-cancellation-probe"
        query = "MATCH (p:Publication) WHERE p.id <> 'pauk-mcp-cancellation-probe' "
        query += "UNWIND range(1, 100000000) AS i RETURN sum(i) AS total"
        await Neo4jReader(self.driver, "neo4j").query("EXPLAIN " + query, {})
        await self._probe_transactions(marker)
        await self._run_cancelled_probe(query, marker)
        deadline = time.monotonic() + 2
        while True:
            rows = await self._probe_transactions(marker)
            if not rows or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(rows, [], "Cancelled synthetic query still has an active server transaction")

    async def test_repository_search_and_reverse_publication_lookup(self) -> None:
        by_url = await self.service.search_repositories(
            SearchRepositoriesRequest(url="http://www.github.com/PAUK-TEST/code.git/")
        )
        self.assertEqual([item.repository_id for item in by_url.items], ["r-code"])
        messy = await self.service.search_repositories(
            SearchRepositoriesRequest(url="https://github.com/pauk-test/messy")
        )
        self.assertEqual([item.repository_id for item in messy.items], ["r-messy"])
        self.assertIsNone(messy.items[0].name.value)
        doi_port = await self.service.search_repositories(
            SearchRepositoriesRequest(url="https://doi.org/10.1234/foo:80/bar")
        )
        self.assertEqual([item.repository_id for item in doi_port.items], ["r-doi-port"])
        priv = await self.service.search_repositories(SearchRepositoriesRequest(query="PrivateHost"))
        self.assertEqual([item.repository_id for item in priv.items], ["r-private"])
        self.assertIsNone(priv.items[0].url)
        nameless = await self.service.search_repositories(
            SearchRepositoriesRequest(url="https://github.com/pauk-test/nameless-repo")
        )
        self.assertEqual([item.repository_id for item in nameless.items], ["r-url-only"])
        dep = await self.service.search_repositories(SearchRepositoriesRequest(query="Dependency"))
        self.assertEqual([item.repository_id for item in dep.items], ["r-dependency"])

        page1 = await self.service.get_repository_publications(
            RepositoryPublicationsRequest(repository_id="old-multi", relation="implements", page_size=1)
        )
        self.assertEqual(page1.repository_id, "r-multi")
        self.assertEqual([item.publication_id for item in page1.items], ["p-multi-1"])
        self.assertIsNotNone(page1.next_cursor)
        page2 = await self.service.get_repository_publications(
            RepositoryPublicationsRequest(repository_id="r-multi", relation="implements", cursor=page1.next_cursor)
        )
        self.assertEqual([item.publication_id for item in page2.items], ["p-multi-2", "p-z-impl"])
        self.assertTrue(all(item.implements and item.verdict is None for item in [*page1.items, *page2.items]))

        mentions = await self.service.get_repository_publications(
            RepositoryPublicationsRequest(repository_id="r-multi", relation="mentions")
        )
        self.assertEqual(
            [(item.publication_id, item.implements, item.verdict) for item in mentions.items],
            [
                ("p-multi-1", True, "accepted"),
                ("p-z-impl", True, "accepted"),
                ("p-a-reuse", False, "rejected"),
                ("p-mixed-mention", False, "rejected"),
                ("p-unknown-mention", False, "unknown"),
            ],
        )

        dep_impl = await self.service.get_repository_publications(
            RepositoryPublicationsRequest(repository_id="r-dependency", relation="implements")
        )
        self.assertEqual(dep_impl.items, [])
        dep_mentions = await self.service.get_repository_publications(
            RepositoryPublicationsRequest(repository_id="r-dependency", relation="mentions")
        )
        self.assertEqual(
            [(item.publication_id, item.implements, item.verdict) for item in dep_mentions.items],
            [("p-paper", False, "rejected")],
        )
        empty_mentions = await self.service.get_repository_publications(
            RepositoryPublicationsRequest(repository_id="r-no-relation", relation="mentions")
        )
        self.assertEqual(empty_mentions.items, [])
