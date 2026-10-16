from typing import LiteralString, cast

from .audit import AuditedNeo4jClient
from .client import Neo4jClient

CONSTRAINTS: list[tuple[str, str]] = [
    ("Person", "id"),
    ("Department", "id"),
    ("Organization", "id"),
    ("Organization", "name_en"),
    ("Publication", "id"),
    ("Repository", "id"),
    ("GitHubProfile", "id"),
    ("GitHubProfile", "login"),
    ("Repository", "url"),
    ("LinkCandidate", "id"),
]


def create_constraints(client: Neo4jClient | AuditedNeo4jClient) -> None:
    """Create all uniqueness constraints listed in CONSTRAINTS.

    Must be called explicitly before loading any data; constructing
    Neo4jClient does not do it.
    """
    with client.driver.session() as session:
        for label, prop in CONSTRAINTS:
            query = cast(LiteralString, f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
            session.run(query)
