"""Domain layer for editing the graph by hand.

Everything that changes the graph outside the pipeline goes through here:
the `pauk admin` commands today, the admin panel's HTTP routes later. The
callers above only parse their arguments and set `actor_context`; what a
valid change *is* lives in this module and nowhere else.

Why a layer at all, rather than calling Neo4jClient from a request handler:

- **Labels and relationship types are interpolated into Cypher.** They are
  identifiers, so the driver cannot bind them as parameters —
  `client.py` builds `f"MERGE (n:{label} ...)"` by hand. While the only
  source of those strings is our own literals, that is safe. The moment the
  source is an HTTP request, a missing whitelist is an injection. Every
  entry point here validates against a closed set built from
  `extract.py::NODE_REGISTRY`, and the client is never called with anything
  else.
- **The same rules must hold for every caller.** A CLI and a web form that
  each validate on their own will drift.
- **Concurrent editors.** Two people opening the same person and saving
  would silently overwrite each other, and the audit log would show two
  legitimate edits. `update_node` takes the `updated_at` the editor saw and
  refuses the write if the node has moved on since.

The whitelists are derived from NODE_REGISTRY rather than written out
again, so a field added to the loader is editable here without a second
edit — and a field that is not published to the graph cannot be set by
hand either.
"""

from __future__ import annotations

import json
import logging

from .client import Neo4jClient
from .extract import JSON_TEXT_FIELDS, NODE_REGISTRY

logger = logging.getLogger(__name__)

# Set by client.py itself on every write; not editable from outside.
RESERVED_FIELDS = frozenset({"id", "created_at", "updated_at"})


def _build_node_fields() -> dict[str, frozenset[str]]:
    """label -> the properties the loader publishes for it.

    Person appears in the registry twice (ITMO and external) with the same
    base label, so the two field lists are unioned.
    """
    fields: dict[str, set[str]] = {}
    for spec in NODE_REGISTRY.values():
        fields.setdefault(spec.labels.split(":")[0], set()).update(spec.prop_fields)
    # The loader publishes created_at for Person, but the database owns it
    # like it owns updated_at. Leaving it in would make `admin schema`
    # advertise a field every write then refuses.
    return {label: frozenset(names - RESERVED_FIELDS) for label, names in fields.items()}


def _build_relationships() -> dict[tuple[str, str, str], str]:
    """(source label, type, target label) -> the property the target is matched by.

    Not every target is found by `id`: a Repository is matched by `url`, a
    GitHubProfile by `login`.
    """
    relationships: dict[tuple[str, str, str], str] = {}
    for spec in NODE_REGISTRY.values():
        source = (spec.rel_src_label or spec.labels).split(":")[0]
        for rel in spec.relationships:
            relationships[(source, rel.rel_type, rel.tgt_label)] = rel.tgt_match_field
    return relationships


NODE_FIELDS = _build_node_fields()
RELATIONSHIPS = _build_relationships()


class MutationError(Exception):
    """A manual edit that must not reach the database."""


class UnknownEntity(MutationError):
    """Label, relationship type or field outside the closed whitelist."""


class NotFound(MutationError):
    """The node the edit targets does not exist."""


class VersionConflict(MutationError):
    """The node changed after the editor last read it."""


def as_stored(props: dict) -> dict:
    """Property values shaped the way the loader stores them.

    Neo4j has no nested-map property type, so `funding`, `versions`,
    `counts_by_year` and `affiliations` are kept as JSON text
    (`extract.py::extract_node` does the same). A manual edit has to match,
    or the driver rejects the write and the same field ends up holding two
    different shapes depending on who wrote it.
    """
    stored = dict(props)
    for key in JSON_TEXT_FIELDS:
        if isinstance(stored.get(key), (list, dict)):
            stored[key] = json.dumps(stored[key], ensure_ascii=False)
    return stored


def validate_label(label: str) -> None:
    if label not in NODE_FIELDS:
        raise UnknownEntity(
            f"unknown node label: {label!r} (known: {', '.join(sorted(NODE_FIELDS))})")


def validate_fields(label: str, props: dict) -> None:
    """Reject anything the loader would not publish for this label.

    Reserved fields are refused separately from unknown ones: they exist,
    but the client owns them, and silently dropping a value someone typed
    would be worse than saying no.
    """
    validate_label(label)
    reserved = RESERVED_FIELDS & props.keys()
    if reserved:
        raise UnknownEntity(f"{', '.join(sorted(reserved))}: set by the database, not editable")
    unknown = props.keys() - NODE_FIELDS[label]
    if unknown:
        raise UnknownEntity(
            f"{label}: unknown field(s) {', '.join(sorted(unknown))} "
            f"(known: {', '.join(sorted(NODE_FIELDS[label]))})")


def validate_relationship(src_label: str, rel_type: str, tgt_label: str) -> str:
    """Check the triple is one the graph actually has, and return the match property."""
    try:
        return RELATIONSHIPS[(src_label, rel_type, tgt_label)]
    except KeyError:
        known = ", ".join(f"({s})-[:{r}]->({t})" for s, r, t in sorted(RELATIONSHIPS))
        raise UnknownEntity(
            f"unknown relationship ({src_label})-[:{rel_type}]->({tgt_label}); known: {known}"
        ) from None


def read_node(client: Neo4jClient, label: str, node_id: str) -> dict:
    """One node's properties, or NotFound."""
    validate_label(label)
    props = client.fetch_node_properties(label, node_id)
    if props is None:
        raise NotFound(f"{label} {node_id} does not exist")
    return props


def create_node(client: Neo4jClient, label: str, node_id: str, props: dict) -> dict:
    """Add a node the pipeline does not know about.

    Such a node survives publishing: the loader only touches ids it has
    rows for, so a hand-made id is never overwritten.

    Raises:
        UnknownEntity: Unknown label or field.
        MutationError: A node with this id already exists — creating it
            again would silently turn into an update.
    """
    validate_fields(label, props)
    if client.fetch_node_properties(label, node_id) is not None:
        raise MutationError(f"{label} {node_id} already exists — edit it instead of creating it")
    client.upsert_nodes_batch(label, [(node_id, as_stored(props))])
    logger.info("created %s %s with %d field(s)", label, node_id, len(props))
    return read_node(client, label, node_id)


def update_node(client: Neo4jClient, label: str, node_id: str, patch: dict,
                expected_updated_at: object | None = None) -> dict:
    """Change fields on an existing node.

    Args:
        client: Graph client — pass the audited wrapper so the change is
            recorded.
        label: Node label, from the whitelist.
        node_id: Which node.
        patch: Fields to set. Only the listed ones change.
        expected_updated_at: The `updated_at` the editor saw when the form
            was opened. When given and the node has moved on since, the
            write is refused instead of overwriting someone else's edit.
            Compared as text: the driver returns its own DateTime type,
            and the value makes a round trip through JSON on the way to a
            browser and back.

    Raises:
        NotFound: No such node.
        VersionConflict: Someone else changed the node in the meantime.
        UnknownEntity: Unknown label or field.
    """
    validate_fields(label, patch)
    current = read_node(client, label, node_id)
    if expected_updated_at is not None and str(current.get("updated_at")) != str(expected_updated_at):
        raise VersionConflict(
            f"{label} {node_id} changed since you opened it "
            f"(now {current.get('updated_at')}, you had {expected_updated_at})")
    client.upsert_nodes_batch(label, [(node_id, as_stored(patch))])
    logger.info("updated %s %s: %s", label, node_id, ", ".join(sorted(patch)))
    return read_node(client, label, node_id)


def delete_node(client: Neo4jClient, label: str, node_id: str, cascade: bool = False) -> int:
    """Remove a node.

    Args:
        cascade: False refuses to delete a node that still has
            relationships — deleting it would silently take edges with it.
            True deletes the node and its relationships.

    Returns:
        Number of nodes deleted (0 or 1).

    Raises:
        NotFound: No such node.
        MutationError: The node has relationships and cascade is False.
    """
    validate_label(label)
    read_node(client, label, node_id)  # raises NotFound
    removed = client.delete_nodes_batch(label, [node_id], detach=cascade)
    if not removed and not cascade:
        raise MutationError(
            f"{label} {node_id} still has relationships; pass cascade to delete them too")
    logger.info("deleted %s %s (cascade=%s)", label, node_id, cascade)
    return removed


def create_relationship(client: Neo4jClient, src_label: str, rel_type: str, tgt_label: str,
                        src_id: str, tgt_id: str, props: dict | None = None) -> int:
    """Connect two existing nodes.

    Returns:
        1 when the relationship was created or updated, 0 when either end
        was not found.
    """
    match_prop = validate_relationship(src_label, rel_type, tgt_label)
    matched = client.upsert_relationships_batch(
        src_label, tgt_label, rel_type, [(src_id, tgt_id, dict(props or {}))], match_prop)
    if not matched:
        raise NotFound(
            f"({src_label} {src_id}) or ({tgt_label} {tgt_id} by {match_prop}) does not exist")
    logger.info("linked (%s %s)-[:%s]->(%s %s)", src_label, src_id, rel_type, tgt_label, tgt_id)
    return matched


def delete_relationship(client: Neo4jClient, src_label: str, rel_type: str, tgt_label: str,
                        src_id: str, tgt_id: str) -> int:
    """Disconnect two nodes, leaving both in place.

    Returns:
        Number of relationships removed (0 if there was none).
    """
    match_prop = validate_relationship(src_label, rel_type, tgt_label)
    removed = client.delete_relationships_batch(
        src_label, tgt_label, rel_type, [(src_id, tgt_id)], match_prop)
    logger.info("unlinked (%s %s)-[:%s]->(%s %s): %d",
                src_label, src_id, rel_type, tgt_label, tgt_id, removed)
    return removed


MERGEABLE = {
    "Person": "merge_person_nodes_batch",
    "Publication": "merge_publication_nodes_batch",
    "Repository": "merge_repository_nodes_batch",
}


def merge_nodes(client: Neo4jClient, label: str, duplicate_id: str, canonical_id: str) -> int:
    """Fold a duplicate into the node it duplicates.

    The duplicate's relationships move to the canonical node and the
    duplicate is deleted. `merged_ids` on the survivor is what keeps the
    duplicate from coming back: the loader reads it (`fetch_merged_id_map`)
    and redirects the old id on every later publish. Without that entry the
    next publish recreates the node.

    **This cannot be undone.** The duplicate is removed with its
    relationships, and the audit diff covers node properties only — the
    edges are gone with no record of what they were. Callers facing a human
    must say so before doing it.

    Returns:
        Number of nodes removed (0 or 1).
    """
    if label not in MERGEABLE:
        raise UnknownEntity(
            f"{label} cannot be merged (mergeable: {', '.join(sorted(MERGEABLE))})")
    if duplicate_id == canonical_id:
        raise MutationError("a node cannot be merged into itself")
    duplicate = read_node(client, label, duplicate_id)
    canonical = read_node(client, label, canonical_id)
    # The duplicate may itself have swallowed ids earlier (A folded into B,
    # now B into C). Those come along, or A stops resolving to anything and
    # the loader recreates it on the next publish.
    merged_ids = list(canonical.get("merged_ids") or [])
    for swallowed in [*(duplicate.get("merged_ids") or []), duplicate_id]:
        if swallowed not in merged_ids and swallowed != canonical_id:
            merged_ids.append(swallowed)
    # Written before the fold: afterwards the duplicate is gone, and a
    # failure between the two steps would leave it free to reappear on the
    # next publish.
    client.upsert_nodes_batch(label, [(canonical_id, {"merged_ids": merged_ids})])
    removed = getattr(client, MERGEABLE[label])([(duplicate_id, canonical_id)])
    logger.info("merged %s %s into %s", label, duplicate_id, canonical_id)
    return removed
