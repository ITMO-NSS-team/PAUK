"""Declarative mapping from prepared JSONL rows (plain dicts) to Neo4j
nodes/relationships.

Works on plain dicts, not pydantic instances — extract_node uses an explicit
property whitelist (NodeSpec.prop_fields), so fields like `_processing`
(a dict of ProcessingState, not a Neo4j-safe primitive) or any other field
not listed are simply never copied into node properties. Nothing needs to
special-case them.

This module assumes today's flat, per-entity prepared JSONL layout
(persons.jsonl, departments.jsonl, publications.jsonl, ...). It does not
handle a nested/aggregate prepared-JSONL format.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field


# Row fields holding nested maps: Neo4j has no nested-map property type, so
# they are stored as JSON text.
JSON_TEXT_FIELDS = ("funding", "versions", "counts_by_year")


@dataclass(frozen=True)
class RelSpec:
    """Describes one relationship embedded in a prepared-JSONL row.

    Attributes:
        field: Key on the source dict holding the relationship data.
        rel_type: Cypher relationship type to create, e.g. "BELONGS_TO".
        tgt_label: Label of the relationship's target node.
        tgt_id_field: Key inside each list item holding the target id.
            None means the list items are the id themselves (e.g. a plain
            id-reference list like department_ids).
        prop_fields: Keys copied from each item onto the relationship as
            properties.
        tgt_match_field: Property used to look up the target node. Not
            always "id" — e.g. Repository is matched by "url".
        scalar: True if `field` holds a single value (str | None) instead
            of a list, e.g. Repository.owner_login.
        guard: Optional per-item filter, used to split a single field into
            several relationship types (a discriminated union), e.g.
            MentionsLink.target_kind.
    """

    field: str
    rel_type: str
    tgt_label: str
    tgt_id_field: str | None
    prop_fields: tuple[str, ...] = ()
    tgt_match_field: str = "id"
    scalar: bool = False
    guard: Callable[[dict], bool] | None = None


@dataclass(frozen=True)
class NodeSpec:
    """Describes how to turn one prepared-JSONL row into a Neo4j node.

    Attributes:
        labels: Cypher label(s) for the node, e.g. "Person:Itmo".
        id_field: Key on the row holding the node's id.
        prop_fields: Whitelist of keys copied as plain node properties.
            Any other key on the row (e.g. `_processing`) is ignored.
        relationships: Relationships embedded in the row, extracted
            separately from prop_fields.
        rel_src_label: Label(s) used to MATCH this node as a relationship
            source. Defaults to `labels`; persons override it with the base
            "Person" because a person's extra label (Itmo/External) can be
            upgraded by a later group while their relationships must still
            resolve.
    """

    labels: str
    id_field: str = "id"
    prop_fields: tuple[str, ...] = ()
    relationships: tuple[RelSpec, ...] = field(default_factory=tuple)
    rel_src_label: str | None = None


NODE_REGISTRY: dict[str, NodeSpec] = {
    "department": NodeSpec(
        labels="Department",
        prop_fields=("name_en", "name_ru", "name_variants"),
    ),
    "itmo_person": NodeSpec(
        labels="Person:Itmo",
        rel_src_label="Person",
        prop_fields=(
            "openalex_id",
            "orcid",
            "name_en",
            "name_variants",
            "email",
            "first_name_ru",
            "second_name_ru",
            "surname_ru",
            "degree",
            "github",
            "google_scholar",
            "openreview",
            "thesis",
            "scopus_id",
            "researcher_id",
            "dblp_id",
            "name_ru",
            "other_names",
            "biography",
            "country",
            "homepage",
            "gitlab_username",
            "linkedin",
            "twitter",
            "wikipedia",
            "works_count",
            "cited_by_count",
            "h_index",
            "i10_index",
            "counts_by_year",
            "status",
            "created_at",
            "enriched_at",
            "merged_ids",
        ),
        relationships=(
            RelSpec("department_ids", "BELONGS_TO", "Department", None),
            RelSpec(
                "authored",
                "AUTHORED",
                "Publication",
                "publication_id",
                ("position", "affiliation", "is_corresponding"),
            ),
            RelSpec(
                "contributed_to",
                "CONTRIBUTED_TO",
                "Repository",
                "repository_id",
                ("role",),
            ),
        ),
    ),
    "external_person": NodeSpec(
        labels="Person:External",
        rel_src_label="Person",
        prop_fields=(
            "openalex_id",
            "orcid",
            "name_en",
            "name_variants",
            "email",
            "scopus_id",
            "researcher_id",
            "dblp_id",
            "name_ru",
            "other_names",
            "biography",
            "country",
            "homepage",
            "gitlab_username",
            "linkedin",
            "twitter",
            "wikipedia",
            "works_count",
            "cited_by_count",
            "h_index",
            "i10_index",
            "counts_by_year",
            "status",
            "created_at",
            "enriched_at",
            "merged_ids",
        ),
        relationships=(
            RelSpec(
                "authored",
                "AUTHORED",
                "Publication",
                "publication_id",
                ("position", "affiliation", "is_corresponding"),
            ),
        ),
    ),
    "publication": NodeSpec(
        labels="Publication",
        prop_fields=(
            "title",
            "journal",
            "doi",
            "publication_date",
            "year",
            "has_code",
            "code_url",
            "funding",
            "openalex_url",
            "pdf_url",
            "abstract",
            "versions",
        ),
        relationships=(
            RelSpec("department_ids", "PRODUCED_BY", "Department", None),
            RelSpec(
                "mentions_links",
                "MENTIONS_LINK",
                "Repository",
                "repository_url",
                ("context", "page_number", "is_relevant", "llm_confidence", "llm_reason"),
                tgt_match_field="url",
                guard=lambda item: item.get("target_kind") == "repository",
            ),
            RelSpec(
                "mentions_links",
                "MENTIONS_LINK",
                "LinkCandidate",
                "candidate_id",
                ("context", "page_number", "is_relevant", "llm_confidence", "llm_reason"),
                guard=lambda item: item.get("target_kind") == "candidate",
            ),
        ),
    ),
    "repository": NodeSpec(
        labels="Repository",
        prop_fields=(
            "name",
            "url",
            "github_id",
            "cited_urls",
            "description",
            "access_date",
            "has_readme",
            "stars_num",
            "last_updated",
            "license",
            "contributors",
        ),
        relationships=(
            RelSpec("department_ids", "DEVELOPED_BY", "Department", None),
            RelSpec("publication_ids", "IMPLEMENTS", "Publication", None),
            RelSpec(
                "owner_login",
                "OWNED_BY",
                "GitHubProfile",
                None,
                tgt_match_field="login",
                scalar=True,
            ),
        ),
    ),
    "github_profile": NodeSpec(
        labels="GitHubProfile",
        prop_fields=("login", "name", "html_url", "description", "location", "type"),
    ),
    "link_candidate": NodeSpec(
        labels="LinkCandidate",
        prop_fields=("url", "host"),
    ),
}


def extract_node(row: dict, spec: NodeSpec) -> tuple[str, tuple[str, dict]]:
    """Extract a single node from a prepared-JSONL row.

    Args:
        row: One decoded JSONL line.
        spec: The NodeSpec describing which keys are node properties.

    Returns:
        A (labels, (node_id, properties)) tuple, shaped for
        Neo4jClient.upsert_nodes_batch.
    """
    node_id = row[spec.id_field]
    props = {k: row[k] for k in spec.prop_fields if row.get(k) is not None}
    for key in JSON_TEXT_FIELDS:
        if isinstance(props.get(key), (list, dict)):
            props[key] = json.dumps(props[key], ensure_ascii=False)
    return spec.labels, (node_id, props)


def extract_relationships(row: dict, spec: NodeSpec) -> dict[tuple[str, str, str, str], list[tuple[str, str, dict]]]:
    """Extract every relationship embedded in a prepared-JSONL row.

    Args:
        row: One decoded JSONL line.
        spec: The NodeSpec describing which keys hold relationship data.

    Returns:
        A dict keyed by (src_label, tgt_label, rel_type, tgt_match_field),
        mapping to a list of (src_id, tgt_id, rel_props) tuples. The key
        includes tgt_match_field because the same rel_type (e.g.
        MENTIONS_LINK) can match its target by different properties
        depending on the RelSpec (url for Repository, id for
        LinkCandidate) — those can't share one batch.
    """
    src_id = row[spec.id_field]
    src_label = spec.rel_src_label or spec.labels
    out: dict[tuple[str, str, str, str], list[tuple[str, str, dict]]] = {}

    for rel in spec.relationships:
        key = (src_label, rel.tgt_label, rel.rel_type, rel.tgt_match_field)

        if rel.scalar:
            value = row.get(rel.field)
            if value is None:
                continue
            out.setdefault(key, []).append((src_id, value, {}))
            continue

        for item in row.get(rel.field) or []:
            if rel.tgt_id_field is None:
                out.setdefault(key, []).append((src_id, item, {}))
                continue
            if rel.guard is not None and not rel.guard(item):
                continue
            tgt_id = item.get(rel.tgt_id_field)
            if tgt_id is None:
                continue
            props = {k: item[k] for k in rel.prop_fields if item.get(k) is not None}
            out.setdefault(key, []).append((src_id, tgt_id, props))

    return out
