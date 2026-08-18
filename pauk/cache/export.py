from __future__ import annotations

import logging
import time
from pathlib import Path

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from pauk.settings import Settings

from .graph_snapshot import write_snapshot

logger = logging.getLogger(__name__)

CYPHER_RETRIES = 5


def _execute_retrying(driver, query, **params):
    attempt = 0
    while True:
        attempt += 1
        try:
            t0 = time.time()
            records, _, _ = driver.execute_query(query, **params)
            logger.info(
                "  %d   %.1f c: %s…",
                len(records),
                time.time() - t0,
                query.lstrip()[:60],
            )
            return records
        except (ServiceUnavailable, SessionExpired, TransientError, OSError) as exc:
            if attempt == CYPHER_RETRIES:
                raise
            wait = min(60, 5 * attempt)
            logger.warning(
                "  (%s: %s),  %d/%d,  %d c",
                type(exc).__name__,
                exc,
                attempt,
                CYPHER_RETRIES,
                wait,
            )
            time.sleep(wait)


def cypher(driver, query, **params) -> list[tuple]:
    """Retrying read, rows as positional tuples."""
    return [tuple(r.values()) for r in _execute_retrying(driver, query, **params)]


def cypher_dict(driver, query, **params) -> list[dict]:
    """Retrying read, rows keyed by their Cypher column names — used where a
    row's shape is expected to grow (persons), so a new column needs no
    positional bookkeeping anywhere else."""
    return [r.data() for r in _execute_retrying(driver, query, **params)]


def load_db(driver) -> dict[str, list]:
    """Read everything into the same flat structures build_graph_data() expects.
    Author departments and repo owners aren't plain columns in the graph model —
    they're relationships: (:Person:Itmo)-[:BELONGS_TO]->(:Department),
    (:Repository)-[:OWNED_BY]->(:GitHubProfile)."""
    db: dict[str, list] = {}

    db["persons"] = cypher_dict(
        driver,
        "MATCH (p:Person:Itmo) "
        "RETURN p.id AS id, p.first_name_ru AS first_name_ru, "
        "       p.second_name_ru AS second_name_ru, p.surname_ru AS surname_ru, "
        "       p.name_ru AS name_ru, p.name_variants AS name_variants, "
        "       p.name_en AS name_en, p.degree AS degree, p.github AS github, "
        "       p.orcid AS orcid",
    )

    db["publications"] = cypher(
        driver,
        "MATCH (pub:Publication) "
        "RETURN pub.id AS id, pub.title AS title, pub.journal AS journal, "
        "       pub.doi AS doi, toString(pub.publication_date) AS publication_date, "
        "       pub.year AS year, pub.has_code AS has_code, pub.code_url AS code_url",
    )

    db["repositories"] = cypher(
        driver,
        "MATCH (r:Repository) "
        "OPTIONAL MATCH (r)-[:OWNED_BY]->(gh:GitHubProfile) "
        "RETURN r.id AS id, r.name AS name, r.url AS url, "
        "       r.description AS description, r.stars_num AS stars_num, gh.login AS owner",
    )

    db["departments"] = cypher_dict(
        driver,
        "MATCH (d:Department) RETURN d.id AS id, d.name_ru AS name_ru, d.name_en AS name_en",
    )

    db["authorship"] = cypher(
        driver,
        "MATCH (p:Person:Itmo)-[:AUTHORED]->(pub:Publication) RETURN pub.id AS pid, p.id AS per",
    )

    db["person_depts"] = cypher(
        driver,
        "MATCH (p:Person:Itmo)-[:BELONGS_TO]->(d:Department) RETURN p.id AS per, d.id AS did",
    )

    db["pub_depts"] = cypher(
        driver,
        "MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) RETURN pub.id AS pid, d.id AS did ORDER BY d.id",
    )

    db["repo_pubs"] = cypher(
        driver,
        "MATCH (r:Repository)-[:IMPLEMENTS]->(pub:Publication) RETURN r.id AS rid, pub.id AS pid",
    )

    db["repo_persons"] = cypher(
        driver,
        "MATCH (p:Person:Itmo)-[rel:CONTRIBUTED_TO]->(r:Repository) RETURN r.id AS rid, p.id AS per, rel.role AS role",
    )

    db["repo_depts"] = cypher(
        driver,
        "MATCH (r:Repository)-[:DEVELOPED_BY]->(d:Department) RETURN r.id AS rid, d.id AS did ORDER BY d.id",
    )

    return db


class GraphSnapshotExporter:
    def __init__(self, config: Settings) -> None:
        self.config = config

    def export(self, path: Path | None = None) -> Path:
        if not self.config.neo4j_password:
            raise ValueError("Neo4j password is empty - set NEO4J_PASSWORD in .env")

        target = path or self.config.cache_dir / "graph_snapshot.json"
        driver = GraphDatabase.driver(
            self.config.neo4j_uri,
            auth=(self.config.neo4j_user, self.config.neo4j_password),
        )
        try:
            driver.verify_connectivity()
            write_snapshot(target, load_db(driver))
        finally:
            driver.close()
        return target
