from typing import LiteralString, cast

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


def create_constraints(client: Neo4jClient) -> None:
    """Create all uniqueness constraints listed in CONSTRAINTS.

    Must be called explicitly before loading any data — it is not a side
    effect of constructing Neo4jClient.

    Args:
        client: An open Neo4jClient to run the constraint statements on.
    """
    with client.driver.session() as session:
        for label, prop in CONSTRAINTS:
            query = cast(LiteralString, f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
            session.run(query)
