"""Opt-in synthetic Neo4j checks in a new disposable container, never an existing graph."""

import asyncio
import os
import subprocess
import time
import unittest
import uuid

from neo4j import AsyncGraphDatabase, GraphDatabase

from pauk.mcp.contracts import ExplainRequest, MCPError, PublicationRequest, RepositoryRequest, SearchRequest
from pauk.mcp.reader import Neo4jReader
from pauk.mcp.service import Service

SYNTHETIC_PASSWORD = "pauk-mcp-synthetic-only-password"
SEED = """
CREATE (p:Publication {id:'p-paper', merged_ids:['old-paper'], title:'Synthetic PAUK paper',
                       doi:'https://doi.org/10.1234/PAUK-TEST', year:2026}),
       (p2:Publication {id:'p-other', title:'Literal .* test', doi:'10.1234/pauk-test-extra'}),
       (r:Repository {id:'r-code', merged_ids:['old-code', 'ambiguous'], url:'https://github.com/pauk-test/code',
                      name:'Synthetic code', description:'PRIVATE_CANARY', language:'Python', license:'MIT',
                      archived:false, last_updated:datetime('2026-01-02T00:00:00Z')}),
       (d:Repository {id:'r-dependency', merged_ids:['ambiguous'], name:'Dependency'}),
       (u:Repository {id:'r-unknown'}), (s:Repository {id:'r-scalar'}),
       (m:Repository {id:'r-misaligned'}), (n:Repository {id:'r-no-relation'}),
       (short:Repository {id:'r-short-source'}), (long:Repository {id:'r-long-source'}),
       (r)-[:IMPLEMENTS]->(p),
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


@unittest.skipUnless(os.getenv("PAUK_MCP_INTEGRATION") == "1", "Opt-in disposable synthetic Neo4j test")
class GraphTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def _start_container(cls) -> None:
        cls.container = "pauk-mcp-isolated-" + uuid.uuid4().hex[:12]
        subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--pull",
                "never",
                "--name",
                cls.container,
                "-p",
                "127.0.0.1::7687",
                "-e",
                "NEO4J_AUTH=neo4j/" + SYNTHETIC_PASSWORD,
                "-e",
                "NEO4J_server_memory_heap_initial__size=256m",
                "-e",
                "NEO4J_server_memory_heap_max__size=256m",
                "neo4j:2026.05.0",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls._start_container()
        cls.addClassCleanup(cls.stop_container)
        port = (
            subprocess.check_output(["docker", "port", cls.container, "7687/tcp"], text=True).strip().rsplit(":", 1)[1]
        )
        cls.uri = "bolt://127.0.0.1:" + port
        with GraphDatabase.driver(
            cls.uri, auth=("neo4j", SYNTHETIC_PASSWORD), connection_timeout=1, max_transaction_retry_time=0
        ) as driver:
            deadline = time.monotonic() + 70
            while True:
                try:
                    driver.verify_connectivity()
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Disposable Neo4j startup timed out") from None
                    time.sleep(0.5)
            with driver.session(database="neo4j") as session:
                session.run(SEED).consume()

    @classmethod
    def stop_container(cls) -> None:
        """Stop only the exact new container owned by this test class."""
        subprocess.run(["docker", "stop", "--time", "5", cls.container], check=True, capture_output=True)

    async def asyncSetUp(self) -> None:
        self.driver = AsyncGraphDatabase.driver(
            self.uri, auth=("neo4j", SYNTHETIC_PASSWORD), max_transaction_retry_time=0
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
