import logging
from typing import LiteralString, cast

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2000


def chunked(seq: list, size: int = CHUNK_SIZE):
    """Yield successive slices of a sequence.

    Args:
        seq: The list to split into chunks.
        size: Maximum number of items per chunk.

    Yields:
        Successive sub-lists of `seq`, each at most `size` items long.
    """
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class Neo4jClient:
    """Thin wrapper around batched node/relationship upserts into Neo4j.

    The constructor only opens a driver connection — it does not create
    constraints. Call schema.create_constraints() explicitly before loading
    any data (see pauk/graph/schema.py).
    """

    def __init__(self, uri: str, user: str, password: str):
        """Open a Neo4j driver connection.

        Args:
            uri: Bolt connection URI, e.g. "bolt://localhost:7687".
            user: Neo4j username.
            password: Neo4j password.
        """
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        """Close the underlying driver connection."""
        self.driver.close()

    def upsert_nodes_batch(self, labels: str | list[str], nodes: list[tuple[str, dict]]):
        """Create or update a batch of nodes in one query.

        Args:
            labels: A single label ("Person") or a list of labels to
                combine (["Person", "Itmo"]).
            nodes: List of (node_id, properties) tuples.
        """
        if not nodes:
            return

        label_str = ":".join(labels) if isinstance(labels, list) else labels

        batch = []
        for node_id, properties in nodes:
            props_clean = {k: v for k, v in properties.items() if k not in ("id", "created_at", "updated_at")}
            batch.append({"node_id": node_id, "properties": props_clean})

        # Cypher can't parameterize identifiers (labels), only literal values,
        # so the label has to be interpolated into the query text itself.
        # label_str always comes from our own NODE_REGISTRY/CONSTRAINTS, never
        # from row data, so this is safe despite not being a LiteralString.
        query = cast(
            LiteralString,
            f"""
            UNWIND $batch AS row
            MERGE (n:{label_str} {{id: row.node_id}})
            ON CREATE SET n += row.properties, n.created_at = datetime(), n.updated_at = datetime()
            ON MATCH SET  n += row.properties, n.updated_at = datetime()
            """,
        )

        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, batch=batch))

    def upsert_person_nodes_batch(self, nodes: list[tuple[str, dict]], is_itmo: bool):
        """Create or update a batch of Person nodes in one query.

        Persons are merged on the base :Person label because the same author
        can appear as ITMO in one group and external in another — merging on
        the full label pair would create a duplicate node and violate the
        Person.id uniqueness constraint. The :Itmo label is sticky: at least
        one ITMO affiliation anywhere makes the person ITMO, so an external
        row never downgrades an existing :Itmo node.

        Args:
            nodes: List of (node_id, properties) tuples.
            is_itmo: Whether this batch carries ITMO persons.
        """
        if not nodes:
            return

        batch = []
        for node_id, properties in nodes:
            props_clean = {k: v for k, v in properties.items() if k not in ("id", "created_at", "updated_at")}
            batch.append({"node_id": node_id, "properties": props_clean})

        if is_itmo:
            label_clause = "SET n:Itmo REMOVE n:External"
        else:
            label_clause = "FOREACH (_ IN CASE WHEN n:Itmo THEN [] ELSE [1] END | SET n:External)"

        query = cast(
            LiteralString,
            f"""
            UNWIND $batch AS row
            MERGE (n:Person {{id: row.node_id}})
            ON CREATE SET n += row.properties, n.created_at = datetime(), n.updated_at = datetime()
            ON MATCH SET  n += row.properties, n.updated_at = datetime()
            {label_clause}
            """,
        )

        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, batch=batch))

    def promote_link_candidates_batch(self, candidates: list[tuple[str, str]]) -> None:
        """Replace resolved LinkCandidates with their Repository targets.

        A publish performed while GitHub enrichment failed creates a
        LinkCandidate. On a later successful retry, preserve any existing
        MENTIONS_LINK properties while moving those relationships to the
        Repository, then remove the candidate if nothing else references it.

        Args:
            candidates: (candidate_id, repository_url) pairs.
        """
        if not candidates:
            return

        batch = [
            {"candidate_id": candidate_id, "repository_url": repository_url}
            for candidate_id, repository_url in candidates
        ]
        move_query = cast(
            LiteralString,
            """
            UNWIND $batch AS row
            MATCH (candidate:LinkCandidate {id: row.candidate_id})
            MATCH (repository:Repository {url: row.repository_url})
            MATCH (publication:Publication)-[old:MENTIONS_LINK]->(candidate)
            MERGE (publication)-[new:MENTIONS_LINK]->(repository)
            ON CREATE SET new += properties(old), new.created_at = coalesce(old.created_at, datetime()), new.updated_at = datetime()
            ON MATCH SET new += properties(old), new.updated_at = datetime()
            DELETE old
            """,
        )
        cleanup_query = cast(
            LiteralString,
            """
            UNWIND $batch AS row
            MATCH (candidate:LinkCandidate {id: row.candidate_id})
            WHERE NOT (candidate)--()
            DELETE candidate
            """,
        )

        def promote(tx):
            tx.run(move_query, batch=batch).consume()
            tx.run(cleanup_query, batch=batch).consume()

        with self.driver.session() as session:
            session.execute_write(promote)

    def upsert_relationships_batch(
        self,
        src_label: str,
        tgt_label: str,
        rel_type: str,
        relationships: list[tuple[str, str, dict]],
        tgt_match_prop: str = "id",
    ) -> int:
        """Create or update a batch of relationships in one query.

        Missing target nodes are not auto-created: if `MATCH` can't find the
        target, that relationship silently doesn't materialize. This method
        makes that observable by counting the batch rows whose source and
        target both matched (`RETURN count(r)`). Neo4j's
        `relationships_created` counter is NOT usable for this: it stays 0
        when MERGE finds an already-existing relationship (re-runs, duplicate
        rows in one batch), which is not an error.

        Args:
            src_label: Label of the source node.
            tgt_label: Label of the target node.
            rel_type: Cypher relationship type to create, e.g. "AUTHORED".
            relationships: List of (src_id, tgt_id, rel_properties) tuples.
            tgt_match_prop: Property used to look up the target node — not
                always "id" (e.g. Repository is matched by "url",
                GitHubProfile by "login").

        Returns:
            Number of batch rows whose relationship was created or updated.
            If lower than len(relationships), some source/target nodes
            weren't found.
        """
        if not relationships:
            return 0

        batch = []
        for src_id, tgt_id, rel_properties in relationships:
            rel_props_clean = {k: v for k, v in rel_properties.items() if k not in ("created_at", "updated_at")}
            batch.append({"src_id": src_id, "tgt_id": tgt_id, "rel_properties": rel_props_clean})

        # Same reasoning as in upsert_nodes_batch: labels/rel_type are
        # interpolated because Cypher can't parameterize identifiers, and
        # they always come from our own code, not from row data.
        query = cast(
            LiteralString,
            f"""
            UNWIND $batch AS row
            MATCH (src:{src_label} {{id: row.src_id}})
            MATCH (tgt:{tgt_label} {{{tgt_match_prop}: row.tgt_id}})
            MERGE (src)-[r:{rel_type}]->(tgt)
            ON CREATE SET r += row.rel_properties, r.created_at = datetime(), r.updated_at = datetime()
            ON MATCH SET  r += row.rel_properties, r.updated_at = datetime()
            RETURN count(r) AS matched
            """,
        )

        with self.driver.session() as session:
            matched = session.execute_write(lambda tx: tx.run(query, batch=batch).single()["matched"])

        if matched < len(batch):
            logger.warning(
                "(:%s)-[:%s]->(:%s): requested %d, matched %d — %d row(s) whose source/target node was not found",
                src_label,
                rel_type,
                tgt_label,
                len(batch),
                matched,
                len(batch) - matched,
            )
        return matched
