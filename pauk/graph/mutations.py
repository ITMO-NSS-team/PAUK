"""Validated graph edits shared by the CLI and admin panel.

Cypher identifiers cannot be bound as parameters, so labels, fields and
relationship types are whitelisted from NODE_REGISTRY. Optional updated_at
checks reject stale node edits. Callers provide the audit actor and source.
"""

from __future__ import annotations

import json
import logging
from typing import get_args, get_origin

from pydantic import AnyHttpUrl, ValidationError

from pauk.models import Department, GitHubProfile, LinkCandidate, Organization, Person, Publication, Repository

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
    # The database owns created_at, so `admin schema` must not advertise it.
    return {label: frozenset(names - RESERVED_FIELDS) for label, names in fields.items()}


def _build_relationships() -> dict[tuple[str, str, str], str]:
    """(source label, type, target label) -> the property the target is matched by.

    Not every target is found by `id`: a Repository is matched by `url`, a
    GitHubProfile by `login`. The source is the base label, taken off `labels`.
    """
    relationships: dict[tuple[str, str, str], str] = {}
    for spec in NODE_REGISTRY.values():
        source = spec.labels.split(":")[0]
        for rel in spec.relationships:
            relationships[(source, rel.rel_type, rel.tgt_label)] = rel.tgt_match_field
    return relationships


NODE_FIELDS = _build_node_fields()
RELATIONSHIPS = _build_relationships()
NODE_LIST_FIELDS = {
    model.__name__: {
        name: get_args(field.annotation)[0]
        for name, field in model.model_fields.items()
        if name in NODE_FIELDS[model.__name__] and get_origin(field.annotation) is list
    }
    for model in (Department, Organization, Person, Publication, Repository, GitHubProfile, LinkCandidate)
}
# The public model keeps code_url as JSON text for compatibility with the map.
NODE_LIST_FIELDS["Publication"]["code_url"] = str

# What the search box looks at: explicit, since these go into Cypher.
SEARCH_FIELDS: dict[str, tuple[str, ...]] = {
    "Person": ("name_en", "name_ru", "orcid"),
    "Publication": ("title", "doi"),
    "Repository": ("url", "name"),
    "GitHubProfile": ("login", "name"),
    "Department": ("name_en", "name_ru"),
    "Organization": ("name_en", "name_ru", "ror_id"),
    "LinkCandidate": ("url", "host"),
}

# Columns shown on top of the searched ones; numbers, not text.
LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "Repository": ("stars_num",),
}

# What orders a listing when nothing is typed: an id order buries the answer.
LIST_ORDER: dict[str, str] = {
    "Repository": "stars_num",
}

SEARCH_LIMIT = 100


def columns(label: str) -> tuple[str, ...]:
    """The fields a listing of this label shows, in the order it shows them."""
    return (*SEARCH_FIELDS[label], *LIST_FIELDS.get(label, ()))


def _check_search_fields() -> None:
    """Reject stale search/list fields at import; Cypher would silently return nulls."""
    for label, fields in SEARCH_FIELDS.items():
        if label not in NODE_FIELDS:
            raise RuntimeError(f"SEARCH_FIELDS names an unknown label: {label}")
        unknown = [name for name in fields if name not in NODE_FIELDS[label] and name != "id"]
        if unknown:
            raise RuntimeError(f"SEARCH_FIELDS[{label}] names fields that do not exist: {unknown}")
    missing = set(NODE_FIELDS) - set(SEARCH_FIELDS)
    if missing:
        raise RuntimeError(f"labels with no search fields: {sorted(missing)}")
    for source, named in (("LIST_FIELDS", LIST_FIELDS),
                          ("LIST_ORDER", {label: (name,) for label, name in LIST_ORDER.items()})):
        for label, fields in named.items():
            if label not in NODE_FIELDS:
                raise RuntimeError(f"{source} names an unknown label: {label}")
            unknown = [name for name in fields if name not in NODE_FIELDS[label]]
            if unknown:
                raise RuntimeError(f"{source}[{label}] names fields that do not exist: {unknown}")


_check_search_fields()


class MutationError(Exception):
    """A manual edit that must not reach the database."""


class UnknownEntity(MutationError):
    """Label, relationship type or field outside the closed whitelist."""


class NotFound(MutationError):
    """The node the edit targets does not exist."""


class VersionConflict(MutationError):
    """The node changed after the editor last read it."""


def as_stored(props: dict) -> dict:
    """Serialize nested properties as JSON text, matching the loader.

    Neo4j cannot store nested maps as node properties.
    """
    stored = dict(props)
    for key in JSON_TEXT_FIELDS:
        if isinstance(stored.get(key), (list, dict)):
            try:
                stored[key] = json.dumps(stored[key], ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                raise MutationError(f"{key}: значение должно быть корректным JSON") from None
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


def _list_value(name: str, value: object, item_type: type) -> list | None:
    """Read model-declared lists without guessing from the current graph value."""
    strings = item_type is str
    message = (f"{name}: укажите одно значение или JSON-список строк" if strings
               else f"{name}: укажите JSON-объект или список объектов")
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.startswith(("[", "{", '"')):
            try:
                value = json.loads(value)
            except ValueError:
                raise MutationError(message) from None
    expected = str if strings else dict
    if isinstance(value, expected):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, expected) for item in value):
        raise MutationError(message)
    return value


def _code_url_text(value: object) -> str | None:
    """Accept one HTTP URL or a list, keeping the GUI's JSON-text format."""
    value = _list_value("code_url", value, str)
    if value is None:
        return None
    message = "code_url: укажите HTTP(S)-ссылку или JSON-список ссылок"
    urls = []
    for url in value:
        url = url.strip()
        try:
            parsed = AnyHttpUrl(url)
            valid = (not parsed.username and not parsed.password
                     and not any(character.isspace() or character < " " or character == "\x7f"
                                 for character in url))
        except ValidationError:
            valid = False
        if not valid:
            raise MutationError(message)
        urls.append(url)
    return json.dumps(urls, ensure_ascii=False)


def normalize_fields(label: str, props: dict) -> dict:
    """Validate manual fields and keep graph values and saved decisions consistent."""
    validate_fields(label, props)
    fields = dict(props)
    for name in fields:
        if label == "Publication" and name == "code_url":
            fields[name] = _code_url_text(fields[name])
        elif name in NODE_LIST_FIELDS[label]:
            fields[name] = _list_value(name, fields[name], NODE_LIST_FIELDS[label][name])
    return as_stored(fields)


def field_values_equal(name: str, before: object, after: object) -> bool:
    """Ignore formatting changes in valid JSON-text containers."""
    if before == after:
        return True
    if name not in JSON_TEXT_FIELDS and name != "code_url":
        return False
    if not isinstance(before, str) or not isinstance(after, str):
        return False
    try:
        first, second = json.loads(before), json.loads(after)
        if not isinstance(first, (list, dict)) or type(first) is not type(second):
            return False
        return (json.dumps(first, sort_keys=True, allow_nan=False)
                == json.dumps(second, sort_keys=True, allow_nan=False))
    except (ValueError, TypeError):
        return False


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


def person_facts(client: Neo4jClient, node_ids: list[str]) -> dict[str, dict]:
    """What a page shows about these people besides their names."""
    return client.fetch_person_facts(node_ids)


def folded_into(client: Neo4jClient, label: str, node_id: str) -> str | None:
    """Resolve an old id from review questions, audit entries or bookmarks.

    The caller has already checked that the id has no node. Returns the survivor's
    id from merged_ids, or None if it cannot be found.
    """
    validate_label(label)
    return client.fetch_canonical_id(label, node_id)


def search_nodes(client: Neo4jClient, label: str, query: str, limit: int = 50,
                 skip: int = 0) -> list[dict]:
    """Find nodes of one label by id or by a piece of their name.

    Reading, not writing, but it goes through this layer for the same
    reason the writes do: the label and the searched field names are
    interpolated into Cypher, so both have to come from a whitelist. The
    fields are `SEARCH_FIELDS`, not whatever the caller asks for.

    Args:
        client: Graph client.
        label: Node label, checked against the whitelist.
        query: Text typed into the search box.
        limit: Rows to return, capped so a wide query cannot pull the
            whole graph into a page.
        skip: Rows to pass over first. Makes pages after the first reachable
            past the cap.

    Raises:
        UnknownEntity: Unknown label.
    """
    validate_label(label)
    query = (query or "").strip()
    # Shown and searched are not the same set.
    fields = list(columns(label))
    capped = min(limit, SEARCH_LIMIT)
    # An empty box means "show me what is there", not "find nothing".
    if not query:
        return client.list_nodes(label, fields, capped, skip, order=LIST_ORDER.get(label))
    return client.search_nodes(label, fields, list(SEARCH_FIELDS[label]), query, capped, skip)


def node_relationships(client: Neo4jClient, label: str, node_id: str) -> list[dict]:
    """Read incident edges with the match_value needed to address the other endpoint.

    Outgoing edges use the triple's target property (id, URL or login).
    Incoming edges use the source id, regardless of the target property.
    """
    validate_label(label)
    rows = client.fetch_node_relationships(label, node_id)
    for row in rows:
        other = row["labels"][0]
        outgoing = row["outgoing"]
        triple = (label, row["type"], other) if outgoing else (other, row["type"], label)
        match_prop = RELATIONSHIPS.get(triple, "id")
        props = row.get("other_props") or {}
        row["match_prop"] = match_prop
        if not outgoing or match_prop == "id":
            row["match_value"] = row["other_id"]
        else:
            row["match_value"] = props.get(match_prop)
    return rows


def snapshot_relationships(client: Neo4jClient, label: str, node_id: str,
                           node_props: dict) -> list[dict]:
    """Capture incident edges as arguments to `create_relationship`.

    Args:
        node_props: Properties read before deleting the node. Incoming edges
            may address it by `url` or `login` rather than by its id.

    Raises:
        MutationError: An edge cannot be restored through the allowed graph
            mutations. Refuse before deleting anything.
    """
    snapshots = []
    for row in node_relationships(client, label, node_id):
        other = row["labels"][0]
        outgoing = row["outgoing"]
        triple = (label, row["type"], other) if outgoing else (other, row["type"], label)
        match_prop = validate_relationship(*triple)
        src_id = node_id if outgoing else row["other_id"]
        tgt_id = row["match_value"] if outgoing else (
            node_id if match_prop == "id" else node_props.get(match_prop))
        if not src_id or not tgt_id:
            raise MutationError(
                f"cannot restore {triple}: missing source id or target {match_prop}")
        snapshots.append({
            "src_label": triple[0], "rel_type": triple[1], "tgt_label": triple[2],
            "src_id": src_id, "tgt_id": tgt_id,
            "props": {name: value for name, value in row["props"].items()
                      if name not in ("created_at", "updated_at")},
        })
    return snapshots


def create_node(client: Neo4jClient, label: str, node_id: str, props: dict) -> dict:
    """Add a node the pipeline does not know about.

    Such a node survives publishing: the loader only touches ids it has
    rows for, so a hand-made id is never overwritten.

    Raises:
        UnknownEntity: Unknown label or field.
        MutationError: A node with this id already exists; creating it
            again would silently turn into an update.
    """
    props = normalize_fields(label, props)
    if client.fetch_node_properties(label, node_id) is not None:
        raise MutationError(f"{label} {node_id} already exists; edit it instead of creating it")
    client.upsert_nodes_batch(label, [(node_id, as_stored(props))])
    logger.info("created %s %s with %d field(s)", label, node_id, len(props))
    return read_node(client, label, node_id)


def update_node(client: Neo4jClient, label: str, node_id: str, patch: dict,
                expected_updated_at: object | None = None) -> dict:
    """Change fields on an existing node.

    Args:
        client: Graph client; pass the audited wrapper so the change is
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
    patch = normalize_fields(label, patch)
    current, updated = client.update_node_properties(
        label, node_id, as_stored(patch), expected_updated_at)
    if current is None:
        raise NotFound(f"{label} {node_id} does not exist")
    if updated is None:
        raise VersionConflict(
            f"{label} {node_id} changed since you opened it "
            f"(now {current.get('updated_at')}, you had {expected_updated_at})")
    logger.info("updated %s %s: %s", label, node_id, ", ".join(sorted(patch)))
    return updated


def delete_node(client: Neo4jClient, label: str, node_id: str, cascade: bool = False) -> int:
    """Remove a node.

    Args:
        cascade: False refuses to delete a node that still has
            relationships; deleting it would silently take edges with it.
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


def read_relationship(client: Neo4jClient, src_label: str, rel_type: str, tgt_label: str,
                      src_id: str, tgt_id: str) -> dict:
    """Read an edge's data for restoration, excluding DB-owned timestamps.

    Raises:
        NotFound: The nodes have no such relationship.
    """
    validate_relationship(src_label, rel_type, tgt_label)
    for row in node_relationships(client, src_label, src_id):
        if (row["outgoing"] and row["type"] == rel_type and tgt_label in row["labels"]
                and row["match_value"] == tgt_id):
            return {name: value for name, value in row["props"].items()
                    if name not in ("created_at", "updated_at")}
    raise NotFound(f"({src_label} {src_id})-[:{rel_type}]->({tgt_label} {tgt_id}) does not exist")


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
    """Move the duplicate's relationships to the survivor, then delete the duplicate.

    merged_ids lets later publishes resolve the old id without recreating it.
    The graph loses edge provenance: only Person folds can be undone from prepared
    rows or pre-merge snapshots, through pauk.graph.unmerge.

    Returns:
        Number of duplicate nodes removed (0 or 1).
    """
    if label not in MERGEABLE:
        raise UnknownEntity(
            f"{label} cannot be merged (mergeable: {', '.join(sorted(MERGEABLE))})")
    if duplicate_id == canonical_id:
        raise MutationError("a node cannot be merged into itself")
    duplicate = read_node(client, label, duplicate_id)
    canonical = read_node(client, label, canonical_id)
    # Ids the duplicate swallowed earlier come along, or they stop resolving.
    merged_ids = list(canonical.get("merged_ids") or [])
    for swallowed in [*(duplicate.get("merged_ids") or []), duplicate_id]:
        if swallowed not in merged_ids and swallowed != canonical_id:
            merged_ids.append(swallowed)
    # Before the fold: afterwards the duplicate is gone and could reappear.
    client.upsert_nodes_batch(label, [(canonical_id, {"merged_ids": merged_ids})])
    removed = getattr(client, MERGEABLE[label])([(duplicate_id, canonical_id)])
    logger.info("merged %s %s into %s", label, duplicate_id, canonical_id)
    return removed


def count_nodes(client: Neo4jClient) -> dict[str, int]:
    """Count each editable node label for the admin overview."""
    counts = {}
    for label in NODE_FIELDS:
        validate_label(label)
        counts[label] = client.count_nodes(label)
    return counts
