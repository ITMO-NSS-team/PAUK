"""Read-only access to the PAUK Neo4j graph.

The search must never change the graph it reads: rebuilding it means a full
publish of every group. Two guards enforce that independently - every query
runs in a read transaction, which Neo4j itself refuses to write in, and a
query that names a write clause is rejected before it is even sent.
"""

from __future__ import annotations

import re
from typing import Any

from neo4j import READ_ACCESS, GraphDatabase

from pauk.settings import Settings

_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b|CALL\s+\{|IN\s+TRANSACTIONS",
    re.IGNORECASE,
)


class WriteQueryRejected(ValueError):
    pass


def assert_read_only(query: str) -> None:
    # Comments and string literals could hide or fake a keyword; strip both.
    stripped = re.sub(r"//[^\n]*|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", " ", query)
    match = _WRITE_CLAUSE.search(stripped)
    if match:
        raise WriteQueryRejected(f"write clause {match.group(0)!r} is not allowed in a read-only search")


class GraphReader:
    def __init__(self, config: Settings) -> None:
        self._driver = GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password))

    def read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        assert_read_only(query)
        with self._driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(lambda tx: tx.run(query, **params).data())

    def close(self) -> None:
        self._driver.close()
