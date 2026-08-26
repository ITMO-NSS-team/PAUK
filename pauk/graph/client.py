import json
import logging
from typing import LiteralString, cast

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2000

# false and [] are meaningful values rather than missing data.  
# Keep this explicit per graph label so newly added fields
# do not accidentally acquire lossy behaviour.
BOOLEAN_MERGE_FIELDS = {
    "Publication": {"has_code"},
    "Repository": {"has_readme"},
}
LIST_MERGE_FIELDS = {
    "Person": {"name_variants", "other_names", "merged_ids"},
    "Publication": {"fields", "merged_ids"},
    "Repository": {"cited_urls", "contributors", "merged_ids"},
}
JSON_LIST_MERGE_FIELDS = {
    "Person": {"affiliations"},
    "Publication": {"funding", "versions"},
}


def _union_values(current: list, extra: list) -> list:
    """Return an order-preserving union that also supports JSON objects."""
    merged = list(current)
    for value in extra:
        if value not in merged:
            merged.append(value)
    return merged


def _merge_duplicate_properties(label: str, canonical: dict, duplicate: dict) -> dict:
    """Build the property updates needed before deleting a duplicate node."""
    updates = {}
    bool_fields = BOOLEAN_MERGE_FIELDS.get(label, set())
    list_fields = LIST_MERGE_FIELDS.get(label, set())
    json_list_fields = JSON_LIST_MERGE_FIELDS.get(label, set())

    for key, duplicate_value in duplicate.items():
        if key in {"id", "created_at", "updated_at"}:
            continue
        canonical_value = canonical.get(key)
        if key in bool_fields:
            if canonical_value is not None or duplicate_value is not None:
                merged = bool(canonical_value) or bool(duplicate_value)
                if merged != canonical_value:
                    updates[key] = merged
        elif key in list_fields:
            if isinstance(duplicate_value, list):
                current = canonical_value if isinstance(canonical_value, list) else []
                merged = _union_values(current, duplicate_value)
                if merged != canonical_value:
                    updates[key] = merged
        elif key in json_list_fields:
            try:
                current = json.loads(canonical_value) if canonical_value is not None else []
                extra = json.loads(duplicate_value) if duplicate_value is not None else []
            except (TypeError, json.JSONDecodeError):
                # A malformed/non-list JSON value is treated as a scalar: a
                # non-null canonical value still wins rather than being lost.
                current = extra = None
            if isinstance(current, list) and isinstance(extra, list):
                merged = _union_values(current, extra)
                encoded = json.dumps(merged, ensure_ascii=False)
                if encoded != canonical_value:
                    updates[key] = encoded
        elif canonical_value is None:
            updates[key] = duplicate_value
    return updates


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

    def __init__(self, uri: str, user: str, password: str,
                 connection_timeout: float | None = None,
                 retry_time: float | None = None):
        """Open a Neo4j driver connection.

        Args:
            uri: Bolt connection URI, e.g. "bolt://localhost:7687".
            user: Neo4j username.
            password: Neo4j password.
            connection_timeout: Seconds to wait for the connection. The
                driver's own default is generous, and a page rendered for a
                person cannot wait that long.
            retry_time: How long a failed transaction keeps being retried.
                Retries suit a batch job; an unreachable database should be
                reported to a waiting person at once.

        Raises:
            ValueError: If password is empty. Settings.neo4j_password
                defaults to "" when NEO4J_PASSWORD isn't set, which would
                otherwise fail late with an opaque driver auth error
                instead of a clear one here.
        """
        if not password:
            raise ValueError("Neo4j password is empty - set NEO4J_PASSWORD in .env")
        options = {}
        if connection_timeout is not None:
            options["connection_timeout"] = connection_timeout
        if retry_time is not None:
            options["max_transaction_retry_time"] = retry_time
        self.driver = GraphDatabase.driver(uri, auth=(user, password), **options)

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

    def _fold_nodes_batch(self, label: str, merges: list[tuple[str, str]],
                          outgoing: tuple[tuple[str, str], ...],
                          incoming: tuple[tuple[str, str], ...] = ()) -> int:
        """Fold duplicate nodes of one label into their canonical node.

        The dedup enrichment stage records the ids it merged away on the
        surviving row (merged_ids). A publish performed before the merge may
        still hold a node for such an id — move its relationships onto the
        canonical node (existing canonical relationships win, the duplicate's
        properties only fill gaps) and delete the duplicate. Rows whose
        duplicate node does not exist are no-ops.

        Args:
            label: Node label being folded, e.g. "Person".
            merges: (duplicate_id, canonical_id) pairs.
            outgoing: (rel_type, target_label) pairs the node points to.
            incoming: (source_label, rel_type) pairs pointing at the node.

        Returns:
            Number of duplicate nodes that were actually removed.
        """
        batch = [
            {"dup_id": dup_id, "canonical_id": canonical_id}
            for dup_id, canonical_id in merges
            if dup_id != canonical_id
        ]
        if not batch:
            return 0

        # Labels and relationship types are interpolated for the same reason
        # as in upsert_relationships_batch: Cypher cannot parameterize
        # identifiers, and these are always our own literals.
        # "SET new += properties(old); SET new += keep" is the pure-Cypher way
        # to fill gaps without letting the duplicate win: everything the old
        # relationship knew is copied in, then the canonical's own values are
        # laid back on top. Without it, a MERGE that finds an existing
        # canonical relationship silently drops the duplicate's properties —
        # e.g. the only AUTHORED edge carrying an affiliation.
        move_queries = [
            cast(
                LiteralString,
                f"""
                UNWIND $batch AS row
                MATCH (dup:{label} {{id: row.dup_id}})-[old:{rel_type}]->(other:{other_label})
                MATCH (canonical:{label} {{id: row.canonical_id}})
                MERGE (canonical)-[new:{rel_type}]->(other)
                ON CREATE SET new.created_at = coalesce(old.created_at, datetime())
                WITH new, old, properties(new) AS keep
                SET new += properties(old)
                SET new += keep
                SET new.updated_at = datetime()
                DELETE old
                """,
            )
            for rel_type, other_label in outgoing
        ] + [
            cast(
                LiteralString,
                f"""
                UNWIND $batch AS row
                MATCH (other:{other_label})-[old:{rel_type}]->(dup:{label} {{id: row.dup_id}})
                MATCH (canonical:{label} {{id: row.canonical_id}})
                MERGE (other)-[new:{rel_type}]->(canonical)
                ON CREATE SET new.created_at = coalesce(old.created_at, datetime())
                WITH new, old, properties(new) AS keep
                SET new += properties(old)
                SET new += keep
                SET new.updated_at = datetime()
                DELETE old
                """,
            )
            for other_label, rel_type in incoming
        ]
        # The nodes themselves cannot use that trick: copying properties(dup)
        # wholesale would momentarily set canonical.id to the duplicate's id
        # while the duplicate node still exists, tripping the uniqueness
        # constraint. The field-specific merge is computed in Python instead.
        props_query = cast(
            LiteralString,
            f"""
            UNWIND $batch AS row
            MATCH (dup:{label} {{id: row.dup_id}})
            MATCH (canonical:{label} {{id: row.canonical_id}})
            RETURN row.dup_id AS dup_id, row.canonical_id AS canonical_id,
                   properties(dup) AS dup_props, properties(canonical) AS canonical_props
            """,
        )
        fill_query = cast(
            LiteralString,
            f"""
            UNWIND $rows AS row
            MATCH (canonical:{label} {{id: row.canonical_id}})
            SET canonical += row.fill
            SET canonical.updated_at = datetime()
            """,
        )
        # The canonical node must exist — otherwise deleting the duplicate
        # would lose the entity entirely.
        delete_query = cast(
            LiteralString,
            f"""
            UNWIND $batch AS row
            MATCH (dup:{label} {{id: row.dup_id}})
            MATCH (:{label} {{id: row.canonical_id}})
            DETACH DELETE dup
            RETURN count(dup) AS removed
            """,
        )

        def merge(tx) -> int:
            records = {(record["dup_id"], record["canonical_id"]): record
                       for record in tx.run(props_query, batch=batch)}
            working_props: dict[str, dict] = {}
            fills: dict[str, dict] = {}
            for row in batch:
                record = records.get((row["dup_id"], row["canonical_id"]))
                if record is None:
                    continue
                canonical_id = record["canonical_id"]
                canonical = working_props.setdefault(canonical_id, dict(record["canonical_props"]))
                fill = _merge_duplicate_properties(label, canonical, record["dup_props"])
                if fill:
                    canonical.update(fill)
                    fills.setdefault(canonical_id, {}).update(fill)
            fill_rows = [{"canonical_id": canonical_id, "fill": fill}
                         for canonical_id, fill in fills.items()]
            for query in move_queries:
                tx.run(query, batch=batch).consume()
            if fill_rows:
                tx.run(fill_query, rows=fill_rows).consume()
            return tx.run(delete_query, batch=batch).single()["removed"]

        with self.driver.session() as session:
            removed = session.execute_write(merge)
        if removed:
            logger.info("%s: folded %d duplicate node(s) into their canonical node", label, removed)
        return removed

    def fetch_persons_for_dedup(self) -> list[dict]:
        """Every Person node with the fields the person merge rules consume.

        publication_ids carries the AUTHORED targets so the rules can
        compute shared coauthors without a second round-trip, and
        department_ids the same for a shared department.
        """
        query = """
            MATCH (p:Person)
            OPTIONAL MATCH (p)-[:AUTHORED]->(w:Publication)
            OPTIONAL MATCH (p)-[:BELONGS_TO]->(d:Department)
            RETURN p.id AS id, p.openalex_id AS openalex_id, p.name_en AS name_en,
                   p.name_variants AS name_variants, p.orcid AS orcid, p.email AS email,
                   p.github AS github, p.openreview AS openreview,
                   p.google_scholar AS google_scholar, p.merged_ids AS merged_ids,
                   'Itmo' IN labels(p) AS is_itmo,
                   collect(DISTINCT w.id) AS publication_ids,
                   collect(DISTINCT d.id) AS department_ids
        """
        with self.driver.session() as session:
            return session.execute_read(
                lambda tx: [dict(record) for record in tx.run(query)])

    def fetch_publications_for_dedup(self) -> list[dict]:
        """Every Publication node with the fields the merge rules consume.

        author_count feeds the ranking (between records of one work the
        best-documented one survives); the bibliographic fields and the
        author list feed the version ledger a fold writes onto the
        canonical node before the duplicate disappears.
        """
        query = """
            MATCH (p:Publication)
            OPTIONAL MATCH (a:Person)-[authored:AUTHORED]->(p)
            RETURN p.id AS id, p.type AS type, p.doi AS doi, p.title AS title,
                   p.journal AS journal, p.publication_date AS publication_date,
                   p.year AS year, p.openalex_url AS openalex_url,
                   p.pdf_url AS pdf_url, p.abstract AS abstract,
                   p.versions AS versions,
                   p.merged_ids AS merged_ids, count(a) AS author_count,
                   collect(CASE WHEN a IS NULL THEN NULL ELSE
                       {person_id: a.id, name: a.name_en, position: authored.position}
                   END) AS authors
        """
        with self.driver.session() as session:
            return session.execute_read(
                lambda tx: [dict(record) for record in tx.run(query)])

    def fetch_publication_fields(self) -> dict[str, set[str]]:
        """Research fields per publication, for the person merge rules."""
        query = """
            MATCH (p:Publication)
            WHERE size(coalesce(p.fields, [])) > 0
            RETURN p.id AS id, p.fields AS fields
        """
        with self.driver.session() as session:
            return session.execute_read(
                lambda tx: {record["id"]: set(record["fields"]) for record in tx.run(query)})

    def fetch_repositories_for_dedup(self) -> list[dict]:
        """Every Repository node with the fields the merge rules consume."""
        query = """
            MATCH (r:Repository)
            OPTIONAL MATCH (r)-[:IMPLEMENTS]->(w:Publication)
            RETURN r.id AS id, r.url AS url, r.github_id AS github_id,
                   r.cited_urls AS cited_urls, r.access_date AS access_date,
                   r.merged_ids AS merged_ids, count(w) AS publication_count
        """
        with self.driver.session() as session:
            return session.execute_read(
                lambda tx: [dict(record) for record in tx.run(query)])

    def fetch_merged_id_map(self, label: str) -> dict[str, str]:
        """Map of merged-away id to canonical id stored on `label` nodes.

        The loader uses this after every publish to re-fold ids that an
        older group's rows just resurrected (the group was published before
        a graph-wide dedup folded those ids elsewhere).
        """
        query = cast(
            LiteralString,
            f"""
            MATCH (n:{label})
            WHERE size(coalesce(n.merged_ids, [])) > 0
            UNWIND n.merged_ids AS merged_id
            RETURN merged_id, n.id AS canonical_id
            """,
        )
        with self.driver.session() as session:
            return session.execute_read(
                lambda tx: {record["merged_id"]: record["canonical_id"] for record in tx.run(query)})

    def merge_person_nodes_batch(self, merges: list[tuple[str, str]]) -> int:
        """Fold duplicate Person nodes into their canonical person."""
        return self._fold_nodes_batch("Person", merges, outgoing=(
            ("AUTHORED", "Publication"),
            ("BELONGS_TO", "Department"),
            ("CONTRIBUTED_TO", "Repository"),
        ))

    def merge_publication_nodes_batch(self, merges: list[tuple[str, str]]) -> int:
        """Fold duplicate Publication nodes into the surviving publication."""
        return self._fold_nodes_batch("Publication", merges, outgoing=(
            ("PRODUCED_BY", "Department"),
            ("MENTIONS_LINK", "Repository"),
            ("MENTIONS_LINK", "LinkCandidate"),
        ), incoming=(
            ("Person", "AUTHORED"),
            ("Repository", "IMPLEMENTS"),
        ))

    def merge_repository_nodes_batch(self, merges: list[tuple[str, str]]) -> int:
        """Fold duplicate Repository nodes into the surviving repository."""
        return self._fold_nodes_batch("Repository", merges, outgoing=(
            ("IMPLEMENTS", "Publication"),
            ("OWNED_BY", "GitHubProfile"),
            ("DEVELOPED_BY", "Department"),
        ), incoming=(
            ("Publication", "MENTIONS_LINK"),
            ("Person", "CONTRIBUTED_TO"),
        ))

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

    def fetch_node_properties(self, label: str, node_id: str) -> dict | None:
        """Every property of one node, or None if there is no such node.

        The dedup fetchers return the few fields they compare on; a manual
        edit needs the whole node — to show it, and to read the
        `updated_at` an optimistic check is made against.

        Args:
            label: Node label, interpolated into Cypher — whitelist only.
            node_id: Value of the node's `id` property.
        """
        query = cast(
            LiteralString,
            f"MATCH (n:{label} {{id: $node_id}}) RETURN properties(n) AS props",
        )
        with self.driver.session() as session:
            row = session.execute_read(lambda tx: tx.run(query, node_id=node_id).single())
        return dict(row["props"]) if row else None

    def count_nodes(self, label: str) -> int:
        """How many nodes carry this label.

        Args:
            label: Node label, interpolated into Cypher — whitelist only.
        """
        query = cast(LiteralString, f"MATCH (n:{label}) RETURN count(n) AS total")
        with self.driver.session() as session:
            row = session.execute_read(lambda tx: tx.run(query).single())
        return int(row["total"]) if row else 0

    def list_nodes(self, label: str, fields: list[str], limit: int = 50) -> list[dict]:
        """The first nodes of a label, in id order.

        What the panel shows before anything is typed: on a small graph it
        is the whole list, on a large one the beginning of it.

        Args:
            label: Node label, interpolated into Cypher — whitelist only.
            fields: Property names to return, also interpolated.
            limit: How many rows to bring back.
        """
        returned = ", ".join(f"n.{name} AS {name}" for name in fields)
        text = f"MATCH (n:{label}) RETURN n.id AS id, {returned} ORDER BY id LIMIT $limit"
        with self.driver.session() as session:
            rows = session.execute_read(
                lambda tx: list(tx.run(cast(LiteralString, text), limit=limit)))
        return [dict(row) for row in rows]

    def search_nodes(self, label: str, fields: list[str], query: str, limit: int = 50) -> list[dict]:
        """Nodes of one label whose text matches, for the panel's search box.

        Case-insensitive substring match across the fields the caller
        names. An exact id always wins and comes first: the panel is
        reached by a link carrying an id at least as often as by typing a
        name, and that lookup must not be buried under fuzzy matches.

        Args:
            label: Node label, interpolated into Cypher — whitelist only.
            fields: Property names to search, also interpolated —
                whitelist only.
            query: What the user typed.
            limit: How many rows to bring back.

        Returns:
            One dict per node: its `id` plus the searched fields.
        """
        conditions = " OR ".join(f"toLower(toString(n.{name})) CONTAINS $needle" for name in fields)
        returned = ", ".join(f"n.{name} AS {name}" for name in fields)
        text = (
            f"MATCH (n:{label}) WHERE n.id = $exact OR {conditions} "
            f"RETURN n.id AS id, {returned}, (n.id = $exact) AS exact "
            f"ORDER BY exact DESC, id LIMIT $limit"
        )
        with self.driver.session() as session:
            rows = session.execute_read(
                lambda tx: list(tx.run(cast(LiteralString, text), needle=query.lower(),
                                       exact=query, limit=limit)))
        return [dict(row) for row in rows]

    def fetch_node_relationships(self, label: str, node_id: str) -> list[dict]:
        """Every edge touching one node, in both directions.

        Direction is reported rather than normalised: the panel has to say
        whether this person authored a publication or a publication was
        produced by this department, and those read differently.

        The other end's properties come along because an id does not always
        address it: two of the eleven relationships are matched by `url` or
        `login`, and removing such an edge needs that value, not the id.
        """
        text = (
            f"MATCH (n:{label} {{id: $node_id}})-[r]-(other) "
            "RETURN type(r) AS type, labels(other) AS labels, other.id AS other_id, "
            "properties(other) AS other_props, "
            "startNode(r).id = n.id AS outgoing "
            "ORDER BY type, other_id"
        )
        with self.driver.session() as session:
            rows = session.execute_read(
                lambda tx: list(tx.run(cast(LiteralString, text), node_id=node_id)))
        return [dict(row) for row in rows]

    def delete_nodes_batch(self, label: str, ids: list[str], detach: bool = True) -> int:
        """Delete nodes by id, optionally taking their relationships with them.

        Nothing in the pipeline deletes a node — the loader only ever
        MERGEs, and dedup folds duplicates rather than removing them. This
        exists for manual removal from the admin layer, which is why it
        reports how many nodes actually went: a caller asking to delete an
        id that is not there must be able to tell.

        Args:
            label: Node label. Interpolated into Cypher, so callers must
                pass a label from a closed whitelist, never user input
                (see pauk/graph/mutations.py).
            ids: Node ids to delete.
            detach: True deletes the node together with its relationships.
                False leaves a node that still has any relationship
                untouched — Neo4j refuses to delete a connected node, and
                that refusal is the point: it stops a careless delete from
                silently tearing edges out of the graph.

        Returns:
            Number of nodes deleted.
        """
        if not ids:
            return 0
        clause = "DETACH DELETE n" if detach else "DELETE n"
        guard = "" if detach else "AND NOT (n)--() "
        query = cast(
            LiteralString,
            f"""
            MATCH (n:{label}) WHERE n.id IN $ids {guard}
            WITH collect(n) AS doomed
            FOREACH (n IN doomed | {clause})
            RETURN size(doomed) AS removed
            """,
        )
        with self.driver.session() as session:
            return session.execute_write(lambda tx: tx.run(query, ids=ids).single()["removed"])

    def delete_relationships_batch(
        self,
        src_label: str,
        tgt_label: str,
        rel_type: str,
        pairs: list[tuple[str, str]],
        tgt_match_prop: str = "id",
    ) -> int:
        """Delete relationships of one type between the given node pairs.

        Args:
            src_label: Label of the source node.
            tgt_label: Label of the target node.
            rel_type: Relationship type to delete, e.g. "AUTHORED".
            pairs: (src_id, tgt_id) pairs whose relationship goes.
            tgt_match_prop: Property the target is looked up by — not
                always "id" (Repository by "url", GitHubProfile by "login").

        Returns:
            Number of relationships deleted.
        """
        if not pairs:
            return 0
        batch = [{"src_id": src_id, "tgt_id": tgt_id} for src_id, tgt_id in pairs]
        query = cast(
            LiteralString,
            f"""
            UNWIND $batch AS row
            MATCH (src:{src_label} {{id: row.src_id}})-[r:{rel_type}]->(tgt:{tgt_label} {{{tgt_match_prop}: row.tgt_id}})
            WITH collect(r) AS doomed
            FOREACH (r IN doomed | DELETE r)
            RETURN size(doomed) AS removed
            """,
        )
        with self.driver.session() as session:
            return session.execute_write(lambda tx: tx.run(query, batch=batch).single()["removed"])
