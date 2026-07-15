from neo4j import GraphDatabase

from scripts.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER


class Neo4jClient:
    # Список нод для создания ограничений уникальности не финальный
    CONSTRAINTS_CONFIG = [
        ("Publication", "id"),
        ("Person", "id"),
        ("Department", "id"),
        ("RepoLink", "id"),
        ("Repository", "id"),
        ("GithubDepartment", "id")
    ]

    def __init__(self, uri, user, password, constraints_config: list[tuple[str, str]] | None = None):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.constraints_config = constraints_config or self.CONSTRAINTS_CONFIG
        self._ensure_constraints()

    def close(self):
        self.driver.close()

    def _ensure_constraints(self):
        with self.driver.session() as session:
            for label, prop in self.constraints_config:
                query = f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
                session.run(query)

    def upsert_nodes_batch(self, labels: str | list[str], nodes: list[tuple[str, dict]]):
        """
        Пакетное создание или обновление узлов.
        :param labels: Строка лейбла ("Person") или список лейблов (["Person", "Author"]).
        :param nodes: Список кортежей (node_id, properties).
        """
        if not nodes:
            return

        label_str = ":".join(labels) if isinstance(labels, list) else labels

        batch = []
        for node_id, properties in nodes:
            props_clean = {k: v for k, v in properties.items() if k not in ('id', 'created_at', 'updated_at')}
            batch.append({'node_id': node_id, 'properties': props_clean})

        query = f"""
        UNWIND $batch AS row
        MERGE (n:{label_str} {{id: row.node_id}})
        ON CREATE SET n += row.properties, n.created_at = datetime(), n.updated_at = datetime()
        ON MATCH SET  n += row.properties, n.updated_at = datetime()
        """

        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, batch=batch))

    def upsert_relationships_batch(self, src_label: str, tgt_label: str, rel_type: str,
                                   relationships: list[tuple[str, str, dict]]):
        """
        Пакетное создание или обновление связей.
        :param relationships: Список кортежей (src_id, tgt_id, rel_properties).
        """
        if not relationships:
            return

        batch = []
        for src_id, tgt_id, rel_properties in relationships:
            rel_props_clean = {k: v for k, v in rel_properties.items() if k not in ('created_at', 'updated_at')}
            batch.append({
                'src_id': src_id,
                'tgt_id': tgt_id,
                'rel_properties': rel_props_clean
            })

        query = f"""
        UNWIND $batch AS row
        MATCH (src:{src_label} {{id: row.src_id}})
        MATCH (tgt:{tgt_label} {{id: row.tgt_id}})
        MERGE (src)-[r:{rel_type}]->(tgt)
        ON CREATE SET r += row.rel_properties, r.created_at = datetime(), r.updated_at = datetime()
        ON MATCH SET  r += row.rel_properties, r.updated_at = datetime()
        """

        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, batch=batch))

    def upsert_node(self, label: str, node_id: str, properties: dict):
        self.upsert_nodes_batch(label, [(node_id, properties)])

    def upsert_relationship(self, src_label: str, src_id: str, tgt_label: str, tgt_id: str,
                            rel_type: str, rel_properties: dict | None = None):
        self.upsert_relationships_batch(src_label, tgt_label, rel_type, [(src_id, tgt_id, rel_properties or {})])


neo4j_driver = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
