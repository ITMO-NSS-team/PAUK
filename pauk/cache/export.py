"""Snapshots the graph from Neo4j into flat structures on disk.

The only place in the `pauk/gui/` chain that actually talks to Neo4j -
everything else (`generate_data.py`, `generate_stats.py` except
`/api/stats`) reads an already-taken snapshot from disk, not the database.

Person queries filter on the `is_itmo` property, not the legacy `:Itmo`
label - ingestion (`pauk/graph/client.py`, `pauk/graph/jsonl_loader.py`)
stopped setting that label after the migration, so a label-based filter
would silently miss any author added since, with no error. Scope stays
ITMO-only on purpose: external co-authors (`Person {is_itmo: false}`) exist
in the graph but aren't pulled in here yet - `generate_data.py` currently
drops any publication with zero ITMO authors, so including them is a
separate decision, not just another Cypher filter.

`created_at`/`updated_at` are NOT the same-named (unpopulated) Pydantic
model fields - they're timestamps `Neo4jClient` stamps on every node write
(`ON CREATE`/`ON MATCH SET ... datetime()` in `pauk/graph/client.py`),
present regardless of the domain model. Read back via `toString()`, like
`publication_date`, since the driver's `neo4j.time.DateTime` isn't
JSON-serializable directly.

`funding`/`versions`/`affiliations` are stored as JSON-text
(`pauk/graph/extract.py::JSON_TEXT_FIELDS` - Neo4j can't hold nested
map/list-of-map). Returned here as-is, an unparsed JSON string - parsing is
left to whoever consumes the snapshot.

Known gap: `pauk/gui/generate_data.py::build_graph_data()` still expects
the old positional-tuple `db` shape (`for pid, per in db["authorship"]`,
`r[0]` on `publications`/`repositories` rows, etc.), while this module has
returned dict rows via `cypher_dict()` since the switchover - feeding a
synthetic dict-shaped `db` straight into `build_graph_data()` raises
`ValueError: too many values to unpack`. `new_generate/graph_builder.py`
already handles the dict shape and replaces `generate_data.py` for real
site generation; fixing `generate_data.py` itself only matters if it still
needs to work standalone.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from pauk.settings import Settings

from .graph_snapshot import dated_snapshot_path, write_snapshot

logger = logging.getLogger(__name__)

CYPHER_RETRIES = 5
"""How many times to retry a query on a transient Neo4j failure before
giving up and propagating the exception."""

CYPHER_RETRY_BACKOFF_STEP_SECONDS = 5
"""Linear backoff step between retries (5, 10, 15, ... seconds)."""

CYPHER_RETRY_MAX_WAIT_SECONDS = 60
"""Upper bound on the wait between retries."""


def _execute_retrying(driver, query, **params):
    """Runs one Cypher query, retrying on transient network failures.

    Transient Neo4j cluster errors (leader re-election, dropped session,
    momentary unavailability) are normal over a long export of ten-plus
    queries, not a reason to fail on the first hiccup.

    Args:
        driver: An open Neo4j driver (`neo4j.Driver`).
        query: Cypher query text.
        **params: Named query parameters, passed through to `driver.execute_query`.

    Returns:
        A list of `neo4j.Record` - raw result rows, not yet turned into
        dicts (`cypher_dict()` does that).

    Raises:
        ServiceUnavailable | SessionExpired | TransientError | OSError:
            if the failure repeats `CYPHER_RETRIES` times with no success.
    """
    attempt = 0
    while True:  # exits only via return (success) or raise (retries exhausted)
        attempt += 1
        try:
            t0 = time.time()
            records, _, _ = driver.execute_query(query, **params)
            logger.info(
                "  %d   %.1f s: %s…",
                len(records),
                time.time() - t0,
                query.lstrip()[:60],
            )
            return records
        except (ServiceUnavailable, SessionExpired, TransientError, OSError) as exc:
            if attempt == CYPHER_RETRIES:
                raise
            wait = min(CYPHER_RETRY_MAX_WAIT_SECONDS, CYPHER_RETRY_BACKOFF_STEP_SECONDS * attempt)
            logger.warning(
                "  (%s: %s),  %d/%d,  %d s",
                type(exc).__name__,
                exc,
                attempt,
                CYPHER_RETRIES,
                wait,
            )
            time.sleep(wait)


def cypher_dict(driver, query, **params) -> list[dict]:
    """Runs a query with retries, rows as dicts keyed by Cypher column name.

    The only way to read rows in this module - a dict lookup by key
    doesn't break when a `RETURN` clause's column order or count changes;
    a positional tuple would break silently.

    Args:
        driver: An open Neo4j driver.
        query: Cypher query text.
        **params: Named query parameters.

    Returns:
        A list of dicts, one per result row, keyed by `RETURN` column alias.

    Example:
        >>> cypher_dict(driver, "MATCH (p:Person {is_itmo: true}) RETURN p.id AS id, p.name_ru AS name_ru")
        [{'id': 'A1', 'name_ru': 'Ivanov'}]
    """
    return [r.data() for r in _execute_retrying(driver, query, **params)]


def load_db(driver) -> dict[str, list]:
    """Reads the whole graph into the flat structures `build_graph_data()` expects.

    Author departments and repository owners are relationships in the
    graph model (`BELONGS_TO`, `OWNED_BY`), not flat columns, so they're
    fetched via separate `OPTIONAL MATCH` queries.

    Full field list and the reasoning per field live as comments right next
    to each `RETURN` line below, not here - so the decision sits next to
    the field it's about.

    Args:
        driver: An open Neo4j driver.

    Returns:
        A flat dict of thirteen keys: `persons`/`publications`/
        `repositories`/`departments`/`organizations`/`authorship`/
        `person_depts`/`pub_depts`/`repo_pubs`/`mentions_repos`/
        `mentions_candidates`/`repo_persons`/`repo_depts`. The first ten
        are what `pauk/gui/generate_data.py::build_graph_data()` expects on
        input; `organizations`/`mentions_repos`/`mentions_candidates` are
        three newer tables with no consumer in existing code yet.
    """
    db: dict[str, list] = {}

    db["persons"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true}) "
        "RETURN "
        # required
        "p.id AS id, "
        # public
        "p.openalex_id AS openalex_id, "
        # both (split into public/private)
        "p.surname_ru AS surname_ru, "
        "p.first_name_ru AS first_name_ru, "
        "p.second_name_ru AS second_name_ru, "
        "p.surname_en AS surname_en, "
        "p.first_name_en AS first_name_en, "
        "p.second_name_en AS second_name_en, "
        # private
        "p.name_ru AS name_ru, "
        "p.name_en AS name_en, "
        "p.name_variants AS name_variants, "
        "p.other_names AS other_names, "
        "p.degree AS degree, "
        "p.github AS github, "
        "p.orcid AS orcid, "
        "p.google_scholar AS google_scholar, "
        "p.openreview AS openreview, "
        "p.email AS email, "
        "p.affiliations AS affiliations, "
        # stubs
        # "p.emails AS emails, " # STUB
        # "p.thesis AS thesis, "  # STUB
        # "p.scopus_id AS scopus_id, "  # STUB
        # "p.researcher_id AS researcher_id, "  # STUB
        # "p.dblp_id AS dblp_id, "  # STUB
        # "p.biography AS biography, "  # STUB
        # "p.country AS country, "  # STUB
        # "p.homepage AS homepage, "  # STUB
        # "p.gitlab_username AS gitlab_username, "  # STUB
        # "p.linkedin AS linkedin, "  # STUB
        # "p.twitter AS twitter, "  # STUB
        # "p.wikipedia AS wikipedia, "  # STUB
        # "p.works_count AS works_count, "  # STUB
        # "p.cited_by_count AS cited_by_count, "  # STUB
        # "p.h_index AS h_index, "  # STUB
        # "p.i10_index AS i10_index, "  # STUB
        # "p.counts_by_year AS counts_by_year, "  # STUB
        # "p.status AS status, "  # STUB
        # "p.enriched_at AS enriched_at, "  # STUB
        # service
        "toString(p.created_at) AS created_at, "
        "toString(p.updated_at) AS updated_at",
    )

    db["publications"] = cypher_dict(
        driver,
        "MATCH (pub:Publication) "
        "RETURN "
        # required
        "pub.id AS id, "
        # public
        "pub.title AS title, "
        "pub.type AS type, "
        "pub.fields AS fields, "
        "pub.journal AS journal, "
        "pub.doi AS doi, "
        "pub.has_code AS has_code, "
        "pub.code_url AS code_url, "
        "pub.funding AS funding, "
        "pub.openalex_url AS openalex_url, "
        "pub.abstract AS abstract, "
        "pub.versions AS versions, "
        "pub.year AS year, "
        "toString(pub.publication_date) AS publication_date, "
        # service
        "toString(pub.created_at) AS created_at, "
        "toString(pub.updated_at) AS updated_at",
    )

    db["repositories"] = cypher_dict(
        driver,
        "MATCH (r:Repository) "
        "OPTIONAL MATCH (r)-[:OWNED_BY]->(gh:GitHubProfile) "
        "RETURN "
        # required
        "r.id AS id, "
        # public
        "r.name AS name, "
        "r.url AS url, "
        "r.description AS description, "
        "r.stars_num AS stars_num, "
        "r.has_readme AS has_readme, "
        "r.license AS license, "
        "r.contributors AS contributors, "
        # "gh.login AS owner, "
        # "gh.name AS owner_name, "  # TODO: decide is it necessary + why not nameS + classification by type
        # "gh.html_url AS owner_html_url, "
        # "gh.description AS owner_description, "
        # "gh.location AS owner_location, "
        # "gh.company AS owner_company, "
        "gh.type AS owner_type, "
        # service
        "toString(r.access_date) AS access_date, "
        # "toString(r.last_updated) AS last_updated, "  # STUB
        "toString(r.created_at) AS created_at, "
        "toString(r.updated_at) AS updated_at",
    )

    db["departments"] = cypher_dict(
        driver,
        "MATCH (d:Department) "
        "OPTIONAL MATCH (d)-[:PART_OF]->(parent) "
        "RETURN "
        # required
        "d.id AS id, "
        # public
        "d.name_ru AS name_ru, "
        "d.name_en AS name_en, "
        "d.name_variants AS name_variants, "
        "d.context_aliases AS context_aliases, "
        "d.kind AS kind",
        # "parent.id AS parent_id, "
        # "labels(parent)[0] AS parent_kind"
    )

    db["organizations"] = cypher_dict(
        driver,
        "MATCH (o:Organization) "
        "RETURN "
        # required
        "o.id AS id, "
        # public
        "o.name_ru AS name_ru, "
        "o.name_en AS name_en, "
        "o.ror_id AS ror_id, "
        "o.country AS country, "
        "o.type AS type",
    )

    db["authorship"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[rel:AUTHORED]->(pub:Publication) "
        "RETURN "
        # required
        "pub.id AS pid, "
        "p.id AS per, "
        # public
        "rel.position AS position, "
        "rel.is_corresponding AS is_corresponding",
    )

    db["person_depts"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[:BELONGS_TO]->(d:Department) "
        "RETURN "
        # required
        "p.id AS per, "
        "d.id AS did",
    )

    db["pub_depts"] = cypher_dict(
        driver,
        "MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) "
        "RETURN "
        # required
        "pub.id AS pid, "
        "d.id AS did "
        "ORDER BY d.id",
    )

    db["repo_pubs"] = cypher_dict(
        driver,
        "MATCH (r:Repository)-[:IMPLEMENTS]->(pub:Publication) "
        "RETURN "
        # required
        "r.id AS rid, "
        "pub.id AS pid",
    )

    db["mentions_repos"] = cypher_dict(
        driver,
        "MATCH (pub:Publication)-[rel:MENTIONS_LINK]->(r:Repository) "
        "RETURN "
        # required
        "pub.id AS pid, "
        "r.id AS rid, "
        # public
        "rel.is_relevant AS is_relevant",
    )

    db["mentions_candidates"] = cypher_dict(
        driver,
        "MATCH (pub:Publication)-[rel:MENTIONS_LINK]->(lc:LinkCandidate) "
        "RETURN "
        # required
        "pub.id AS pid, "
        # public
        "lc.url AS url, "
        "lc.host AS host, "
        "rel.is_relevant AS is_relevant",
    )

    db["repo_persons"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[rel:CONTRIBUTED_TO]->(r:Repository) "
        "RETURN "
        # required
        "r.id AS rid, "
        "p.id AS per, "
        # public
        "rel.role AS role",
    )

    db["repo_depts"] = cypher_dict(
        driver,
        "MATCH (r:Repository)-[:DEVELOPED_BY]->(d:Department) "
        "RETURN "
        # required
        "r.id AS rid, "
        "d.id AS did "
        "ORDER BY d.id",
    )

    return db


class GraphSnapshotExporter:
    """Entry point for `pauk cache export`: Neo4j -> a snapshot file on disk."""

    def __init__(self, config: Settings) -> None:
        """Stores connection config - the driver itself only opens in `export()`.

        Args:
            config: Project settings, in particular `neo4j_uri`/`neo4j_user`/
                `neo4j_password` and `cache_dir` (default snapshot location).
        """
        self.config = config

    def export(self, path: Path | None = None) -> Path:
        """Snapshots the graph from Neo4j and atomically writes it to disk.

        Args:
            path: Where to write the snapshot. Defaults to a dated file in
                `cache_dir` (see `dated_snapshot_path`).

        Returns:
            The final path the snapshot was written to.

        Raises:
            ValueError: the Neo4j password is empty (`NEO4J_PASSWORD` unset)
                - checked before opening the driver, for a clear error
                instead of a late authentication failure on the first query.
        """
        if not self.config.neo4j_password:
            raise ValueError("Neo4j password is empty - set NEO4J_PASSWORD in .env")

        # .resolve() so a relative --output doesn't silently depend on the
        # working directory the command happened to run from.
        target = (path or dated_snapshot_path(self.config.cache_dir)).resolve()
        driver = GraphDatabase.driver(
            self.config.neo4j_uri,
            auth=(self.config.neo4j_user, self.config.neo4j_password),
        )
        try:
            driver.verify_connectivity()  # fail fast with a clear error, not on the first real query
            write_snapshot(target, load_db(driver))
        finally:
            driver.close()
        return target
