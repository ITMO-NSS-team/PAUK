from .client import Neo4jClient

# Лейблы соответствуют реально используемым в extract.py/Cypher — старый
# scripts/neo4j_client.py::CONSTRAINTS_CONFIG ссылался на "RepoLink" и
# "GithubDepartment", которых нет ни в одном реальном запросе (правильно —
# LinkCandidate/GitHubProfile). Publication/Repository/GitHubProfile/
# LinkCandidate регистрируются заранее, хотя пайплайн их ещё не пишет —
# CREATE CONSTRAINT IF NOT EXISTS идемпотентен и бесплатен.
CONSTRAINTS: list[tuple[str, str]] = [
    ("Person", "id"),
    ("Department", "id"),
    ("Publication", "id"),
    ("Repository", "id"),
    ("GitHubProfile", "id"),
    ("LinkCandidate", "id"),
]


def create_constraints(client: Neo4jClient) -> None:
    with client.driver.session() as session:
        for label, prop in CONSTRAINTS:
            session.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
