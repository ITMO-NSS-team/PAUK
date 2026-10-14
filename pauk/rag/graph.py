"""Everything the RAG module asks of Neo4j.

Reads use read-only sessions. Writes touch only `rag_*` properties and
`rag_*` indexes: `pauk publish` updates nodes with `SET n += ...`, so it
leaves them alone, and dropping a profile removes every trace of it.
"""

from __future__ import annotations

import re
from typing import Any

from neo4j import READ_ACCESS, WRITE_ACCESS, GraphDatabase

from pauk.settings import Settings

from .profiles import Profile

_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

PUBLICATIONS = """
MATCH (p:Publication)
RETURN p.id AS id, p.title AS title, p.abstract AS abstract, p.fields AS fields, p.journal AS journal,
       left(p.full_text, 4000) AS full_text, p.type AS type, p[$hash] AS hash
"""
REPOSITORIES = """
MATCH (r:Repository)
RETURN r.id AS id, r.name AS name, r.url AS url, r.description AS description, r.has_readme AS has_readme,
       [(r)-[:IMPLEMENTS]->(p:Publication) WHERE p.title IS NOT NULL | p.title] AS implemented, r[$hash] AS hash
"""


def escape_lucene(text: str) -> str:
    """A question as plain terms: `RT-DETR` must not read as `RT NOT DETR`."""
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


class Graph:
    def __init__(self, config: Settings) -> None:
        self._driver = GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password))

    def close(self) -> None:
        self._driver.close()

    def read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        with self._driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(lambda tx: tx.run(query, **params).data())

    def write(self, query: str, **params: Any) -> list[dict[str, Any]]:
        with self._driver.session(default_access_mode=WRITE_ACCESS) as session:
            return session.execute_write(lambda tx: tx.run(query, **params).data())

    def schema(self, query: str) -> None:
        # Index DDL cannot run inside an explicit transaction.
        with self._driver.session(default_access_mode=WRITE_ACCESS) as session:
            session.run(query).consume()

    # --- indexing -----------------------------------------------------------------

    def nodes(self, label: str, profile: Profile) -> list[dict[str, Any]]:
        query = PUBLICATIONS if label == "Publication" else REPOSITORIES
        return self.read(query, hash=profile.hash_property)

    def store(self, label: str, profile: Profile, rows: list[dict[str, Any]]) -> None:
        """Text, vector and hash per node. `setNodeVectorProperty` keeps the vector as float32."""
        self.write(f"""
            UNWIND $rows AS row
            MATCH (n:{label} {{id: row.id}})
            SET n.`{profile.text_property}` = row.text, n.`{profile.hash_property}` = row.hash
            WITH n, row
            CALL db.create.setNodeVectorProperty(n, $vector, row.vector) 
        """, rows=rows, vector=profile.vector_property)

    def clear(self, label: str, profile: Profile, ids: list[str]) -> None:
        """Nodes that left the profile (now empty text, or filtered out) lose their old entries."""
        self.write(f"""
            UNWIND $ids AS id
            MATCH (n:{label} {{id: id}})
            REMOVE n.`{profile.text_property}`, n.`{profile.vector_property}`, n.`{profile.hash_property}`
        """, ids=ids)

    def ensure_indexes(self, label: str, profile: Profile, dimensions: int, analyzer: str) -> None:
        # IF NOT EXISTS keeps an index built with another analyzer; drop it first.
        for row in self.read("SHOW FULLTEXT INDEXES YIELD name, options WHERE name = $name RETURN options",
                             name=profile.text_index(label)):
            if row["options"]["indexConfig"].get("fulltext.analyzer") != analyzer:
                self.schema(f"DROP INDEX `{profile.text_index(label)}`")
        self.schema(f"""
            CREATE VECTOR INDEX `{profile.vector_index(label)}` IF NOT EXISTS
            FOR (n:{label}) ON n.`{profile.vector_property}`
            OPTIONS {{indexConfig: {{`vector.dimensions`: {int(dimensions)},
                                     `vector.similarity_function`: 'cosine'}}}}
        """)
        self.schema(f"""
            CREATE FULLTEXT INDEX `{profile.text_index(label)}` IF NOT EXISTS
            FOR (n:{label}) ON EACH [n.`{profile.text_property}`]
            OPTIONS {{indexConfig: {{`fulltext.analyzer`: '{analyzer}'}}}}
        """)

    def drop(self, profile: Profile, labels: tuple[str, ...]) -> dict[str, int]:
        removed = {}
        for label in labels:
            for index in (profile.vector_index(label), profile.text_index(label)):
                self.schema(f"DROP INDEX `{index}` IF EXISTS")
            rows = self.write(f"""
                MATCH (n:{label}) WHERE n.`{profile.hash_property}` IS NOT NULL
                REMOVE n.`{profile.text_property}`, n.`{profile.vector_property}`, n.`{profile.hash_property}`
                RETURN count(n) AS n
            """)
            removed[label] = rows[0]["n"]
        return removed

    def rag_indexes(self) -> list[dict[str, Any]]:
        return self.read("""
            SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state, populationPercent
            WHERE name STARTS WITH 'rag_'
            RETURN name, type, labelsOrTypes, properties, state, populationPercent ORDER BY name
        """)

    def await_indexes(self, timeout_seconds: int = 300) -> None:
        self.schema(f"CALL db.awaitIndexes({int(timeout_seconds)})")
        self.schema("CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()")

    # --- retrieval ----------------------------------------------------------------

    def vector_search(self, label: str, index: str, vector: list[float], k: int) -> list[tuple[str, float]]:
        # SEARCH replaces db.index.vector.queryNodes from Neo4j 2026; the index
        # name cannot be a parameter, and profile names are validated identifiers.
        rows = self.read(f"""
            CYPHER 25
            MATCH (n:{label})
            SEARCH n IN (VECTOR INDEX `{index}` FOR $vector LIMIT $k) SCORE AS score
            RETURN n.id AS id, score
        """, vector=vector, k=k)
        return [(row["id"], row["score"]) for row in rows]

    def text_search(self, index: str, terms: str, k: int) -> list[tuple[str, float]]:
        rows = self.read("CALL db.index.fulltext.queryNodes($index, $terms, {limit: $k}) YIELD node, score "
                         "RETURN node.id AS id, score", index=index, terms=terms, k=k)
        return [(row["id"], row["score"]) for row in rows]
