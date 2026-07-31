from .client import Neo4jClient

# Labels match the actual extract.py and Cypher usage.  Constraints are
# created in advance because CREATE CONSTRAINT IF NOT EXISTS is idempotent.
CONSTRAINTS: list[tuple[str, str]] = [
    ("Person", "id"),
    ("Department", "id"),
    ("Publication", "id"),
    ("Repository", "id"),
    ("GitHubProfile", "id"),
    ("GitHubProfile", "login"),
    ("LinkCandidate", "id"),
]


def create_constraints(client: Neo4jClient) -> None:
    with client.driver.session() as session:
        for label, prop in CONSTRAINTS:
            session.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
