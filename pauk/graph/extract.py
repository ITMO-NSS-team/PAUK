"""Declarative mapping from prepared JSONL rows (plain dicts) to Neo4j
nodes/relationships.

Works on plain dicts, not pydantic instances. extract_node copies only the
whitelisted NodeSpec.prop_fields, so fields like `_processing` (not a
Neo4j-safe primitive) are never copied into node properties.

Assumes the flat, per-entity prepared layout (persons, departments,
publications, ...); a nested/aggregate format is not handled.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from pauk.pipeline.membership import ITMO_ROR_ID, normalize_ror, source_observed_at

# Row fields holding nested maps: Neo4j has no nested-map property type, so
# they are stored as JSON text.
JSON_TEXT_FIELDS = (
    "funding", "versions", "counts_by_year", "affiliations", "membership_evidence", "membership_source_coverage"
)


def _membership_source_coverage(row: dict) -> list[dict]:
    """Only complete original-source snapshots may retire earlier claims."""
    works: dict[tuple[str, str], list[dict]] = {}
    source_rows = list(row.get("membership_work_sources") or [])
    represented = {
        (source.get("institution_source_person_id"), source.get("institution_source_id"))
        for source in source_rows
    }
    source_rows.extend(
        authored for authored in row.get("authored") or []
        if (authored.get("institution_source_person_id"), authored.get("institution_source_id")) not in represented
    )
    for authored in source_rows:
        owner, work = authored.get("institution_source_person_id"), authored.get("institution_source_id")
        if owner and work:
            works.setdefault((owner, work), []).append(authored)
    coverage = []
    for (owner, work), records in works.items():
        if any(item.get("institution_rors") is None for item in records):
            continue
        timestamps = [source_observed_at(item.get("institution_observed_at")) for item in records]
        observed = min(timestamps).isoformat() if all(stamp is not None for stamp in timestamps) else None
        coverage.append({"source": "openalex_work", "source_person_id": owner, "source_id": work,
                         "observed_at": observed,
                         "supports_itmo": any(ITMO_ROR_ID in item["institution_rors"] for item in records)})
    state = (row.get("_processing") or row.get("processing") or {}).get("orcid") or {}
    orcid = row.get("orcid")
    if orcid and state.get("request_key") == orcid and state.get("status") in {"completed", "completed_empty"}:
        source_states = [
            item for item in row.get("membership_orcid_sources") or [] if item.get("source_id") == orcid
        ]
        owners = {
            item.get("source_person_id") for item in [*(row.get("affiliations") or []),
                                                       *(row.get("membership_evidence") or [])]
            if item.get("source") == "orcid" and item.get("source_id") == orcid
            and item.get("source_person_id") in {row["id"], *(row.get("merged_ids") or [])}
        }
        owners.update(item["source_person_id"] for item in source_states
                      if item.get("source_person_id") in {row["id"], *(row.get("merged_ids") or [])})
        owner = row["id"] if not row.get("merged_ids") else next(iter(owners)) if len(owners) == 1 else None
        if owner:
            observed_times = [source_observed_at(item.get("observed_at")) for item in source_states
                              if item.get("source_person_id") == owner]
            observed = max((stamp for stamp in observed_times if stamp is not None), default=None)
            if observed is None:
                observed = source_observed_at(state.get("finished_at"))
            coverage.append({"source": "orcid", "source_person_id": owner, "source_id": orcid,
                             "observed_at": observed.isoformat() if observed else None,
                             "supports_itmo": any(
                                 item.get("source") == "orcid" and item.get("source_id") == orcid
                                 and item.get("source_person_id") == owner
                                 and normalize_ror(item.get("ror")) == ITMO_ROR_ID
                                 for item in [*(row.get("affiliations") or []),
                                              *(row.get("membership_evidence") or [])]
                             )})
    return coverage


@dataclass(frozen=True)
class RelSpec:
    """Describes one relationship embedded in a prepared-JSONL row.

    Attributes:
        field: Key on the source dict holding the relationship data.
        rel_type: Cypher relationship type to create, e.g. "BELONGS_TO".
        tgt_label: Label of the relationship's target node.
        tgt_id_field: Key inside each list item holding the target id.
            None means the list items are the id themselves (e.g. department_ids).
        prop_fields: Keys copied from each item onto the relationship as
            properties.
        tgt_match_field: Property used to look up the target node. Not
            always "id": e.g. Repository is matched by "url".
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
        labels: Cypher label(s) for the node, e.g. "Person".
        id_field: Key on the row holding the node's id.
        prop_fields: Whitelist of keys copied as plain node properties.
            Any other key on the row (e.g. `_processing`) is ignored.
        relationships: Relationships embedded in the row, extracted
            separately from prop_fields.
    """

    labels: str
    id_field: str = "id"
    prop_fields: tuple[str, ...] = ()
    relationships: tuple[RelSpec, ...] = field(default_factory=tuple)


NODE_REGISTRY: dict[str, NodeSpec] = {
    "department": NodeSpec(
        labels="Department",
        prop_fields=(
            "name_en",
            "name_ru",
            "name_variants",
            "context_aliases",
            "kind",
            "parent_id",
            "organization_id",
        ),
        # A unit is PART_OF exactly one parent: a sub-unit points at its parent
        # Department (recursive hierarchy), a top-level unit at its Organization.
        relationships=(
            RelSpec("parent_id", "PART_OF", "Department", None, scalar=True),
            RelSpec("organization_id", "PART_OF", "Organization", None, scalar=True),
        ),
    ),
    "organization": NodeSpec(
        labels="Organization",
        prop_fields=("name_en", "name_ru", "ror_id", "country", "type"),
    ),
    "itmo_person": NodeSpec(
        labels="Person",
        prop_fields=(
            "is_itmo",
            "membership_status",
            "membership_policy",
            "membership_evidence",
            "membership_source_coverage",
            "openalex_id",
            "orcid",
            "name_raw",
            "name_variants",
            "email",  # one address for the card
            "emails",  # every known address - github_match identity, not a duplicate
            "first_name_ru",
            "second_name_ru",
            "surname_ru",
            "first_name_en",
            "second_name_en",
            "surname_en",
            "degree",
            "github",
            "google_scholar",
            "thesis",
            "scopus_id",
            "researcher_id",
            "dblp_id",
            "name_ru",
            "name_en",
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
            "affiliations",
            "merged_ids",
        ),
        relationships=(
            RelSpec("department_ids", "BELONGS_TO", "Department", None),
            RelSpec(
                "authored",
                "AUTHORED",
                "Publication",
                "publication_id",
                ("position", "affiliation", "affiliation_source", "is_corresponding",
                 "institution_rors", "institution_source_person_id", "institution_source_id"),
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
        labels="Person",
        prop_fields=(
            "is_itmo",
            "membership_status",
            "membership_policy",
            "membership_evidence",
            "membership_source_coverage",
            "openalex_id",
            "orcid",
            "name_raw",
            "name_variants",
            "email",  # one address for the card
            "emails",  # every known address - github_match identity
            "first_name_ru",
            "second_name_ru",
            "surname_ru",
            "first_name_en",
            "second_name_en",
            "surname_en",
            "degree",
            "github",
            "google_scholar",
            "scopus_id",
            "researcher_id",
            "dblp_id",
            "name_ru",
            "name_en",
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
            "affiliations",
            "merged_ids",
        ),
        relationships=(
            RelSpec(
                "authored",
                "AUTHORED",
                "Publication",
                "publication_id",
                ("position", "affiliation", "affiliation_source", "is_corresponding",
                 "institution_rors", "institution_source_person_id", "institution_source_id"),
            ),
        ),
    ),
    "publication": NodeSpec(
        labels="Publication",
        prop_fields=(
            "title",
            "type",
            "fields",
            "journal",
            "doi",
            "publication_date",
            "year",
            "has_code",
            "code_url",
            "funding",
            "openalex_url",
            "pdf_urls",
            "abstract",
            "full_text",
            "versions",
            "merged_ids",
        ),
        relationships=(
            RelSpec("department_ids", "PRODUCED_BY", "Department", None),
            RelSpec(
                "mentions_links",
                "MENTIONS_LINK",
                "Repository",
                "repository_url",
                (
                    "context",
                    "page_number",
                    "classification_status",
                    "is_relevant",
                    "llm_confidence",
                    "llm_reason",
                ),
                tgt_match_field="url",
                guard=lambda item: item.get("target_kind") == "repository",
            ),
            RelSpec(
                "mentions_links",
                "MENTIONS_LINK",
                "LinkCandidate",
                "candidate_id",
                (
                    "context",
                    "page_number",
                    "classification_status",
                    "is_relevant",
                    "llm_confidence",
                    "llm_reason",
                ),
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
            "topics",
            "language",
            "forks_num",
            "archived",
            "is_fork",
            "contributors",
            "merged_ids",
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
        prop_fields=("login", "name", "html_url", "description", "location", "company", "type"),
    ),
    "link_candidate": NodeSpec(
        labels="LinkCandidate",
        prop_fields=("url", "host"),
    ),
}


def person_spec(row: dict) -> NodeSpec:
    """Which of the two person specs describes this row.

    Persons share one file and one label; `is_itmo` picks the relationship
    whitelist, because an external person never gets BELONGS_TO or
    CONTRIBUTED_TO. Kept here rather than at the call site: a publish and
    an undone merge have to read the same row the same way.
    """
    return NODE_REGISTRY["itmo_person" if row.get("is_itmo") else "external_person"]


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
    if spec.labels == "Person" and "authored" in row:
        props["membership_source_coverage"] = _membership_source_coverage(row)
    for key in JSON_TEXT_FIELDS:
        if isinstance(props.get(key), (list, dict)):
            props[key] = json.dumps(props[key], ensure_ascii=False)
    return spec.labels, (node_id, props)


def extract_relationships(
    row: dict, spec: NodeSpec
) -> dict[tuple[str, str, str, str], list[tuple[str, str, dict]]]:
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
        LinkCandidate), so those can't share one batch.
    """
    src_id = row[spec.id_field]
    src_label = spec.labels
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
