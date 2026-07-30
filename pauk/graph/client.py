import logging

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2000


def chunked(seq: list, size: int = CHUNK_SIZE):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class Neo4jClient:
    """Тонкая обёртка над batch-upsert в Neo4j.

    Конструктор только открывает драйвер — констрейнты создаются отдельно,
    явным вызовом schema.create_constraints(), не как побочный эффект
    импорта/конструирования (см. pauk/graph/schema.py).
    """

    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def upsert_nodes_batch(self, labels: str | list[str], nodes: list[tuple[str, dict]]):
        """
        Пакетное создание или обновление узлов.
        :param labels: Строка лейбла ("Person") или список лейблов (["Person", "Itmo"]).
        :param nodes: Список кортежей (node_id, properties).
        """
        if not nodes:
            return

        label_str = ":".join(labels) if isinstance(labels, list) else labels

        batch = []
        for node_id, properties in nodes:
            props_clean = {k: v for k, v in properties.items() if k not in ("id", "created_at", "updated_at")}
            batch.append({"node_id": node_id, "properties": props_clean})

        query = f"""
        UNWIND $batch AS row
        MERGE (n:{label_str} {{id: row.node_id}})
        ON CREATE SET n += row.properties, n.created_at = datetime(), n.updated_at = datetime()
        ON MATCH SET  n += row.properties, n.updated_at = datetime()
        """

        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, batch=batch))

    def upsert_relationships_batch(
        self,
        src_label: str,
        tgt_label: str,
        rel_type: str,
        relationships: list[tuple[str, str, dict]],
        tgt_match_prop: str = "id",
    ) -> int:
        """
        Пакетное создание или обновление связей.
        :param relationships: Список кортежей (src_id, tgt_id, rel_properties).
        :param tgt_match_prop: свойство, по которому ищется целевой узел —
            не всегда "id" (напр. Repository матчится по "url", GitHubProfile
            по "login").
        :return: сколько связей реально создано/обновлено — если меньше
            len(relationships), часть целевых узлов не найдена (не создаём
            заглушки, только логируем).
        """
        if not relationships:
            return 0

        batch = []
        for src_id, tgt_id, rel_properties in relationships:
            rel_props_clean = {k: v for k, v in rel_properties.items() if k not in ("created_at", "updated_at")}
            batch.append({"src_id": src_id, "tgt_id": tgt_id, "rel_properties": rel_props_clean})

        query = f"""
        UNWIND $batch AS row
        MATCH (src:{src_label} {{id: row.src_id}})
        MATCH (tgt:{tgt_label} {{{tgt_match_prop}: row.tgt_id}})
        MERGE (src)-[r:{rel_type}]->(tgt)
        ON CREATE SET r += row.rel_properties, r.created_at = datetime(), r.updated_at = datetime()
        ON MATCH SET  r += row.rel_properties, r.updated_at = datetime()
        """

        with self.driver.session() as session:
            summary = session.execute_write(lambda tx: tx.run(query, batch=batch).consume())

        created = summary.counters.relationships_created
        if created < len(batch):
            logger.warning(
                "(:%s)-[:%s]->(:%s): запрошено %d, создано %d — %d целевых узлов не найдено",
                src_label, rel_type, tgt_label, len(batch), created, len(batch) - created,
            )
        return created
