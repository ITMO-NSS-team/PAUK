"""Taking a fold apart: the person a merge removed, put back.

`merge_nodes` moves the duplicate's relationships onto the survivor and
deletes it, and the graph keeps no record of which edge came from where.
The prepared rows and durable pre-merge snapshots do. A person's edges are
declared by that person's own row and by nothing else (no spec in the registry points *at* a Person),
so the row says exactly what node the merge removed and exactly what the
survivor took from it.

That is what makes an undo possible, and why it is built out of the
loader's own parts (`person_spec`, `extract_node`, `extract_relationships`).
A fold that came apart differently from the way it was built would be
worse than no undo at all.

Two things it cannot give back. An edge somebody added by hand is written
down nowhere, so an edge neither row claims stays on the survivor rather
than being guessed at. And a manual field edit that the fold had covered
comes back from `apply_overrides` at the end, the same way a publish
restores it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from pymongo.database import Database

from pauk.models import ExternalIdentifier, Person
from pauk.pipeline.normalize import _merge_person
from pauk.storage.person_merge_sources import snapshots
from pauk.storage.prepared import PreparedStore

from .client import _merge_duplicate_properties, chunked
from .extract import extract_node, extract_relationships, person_spec
from .mutations import (
    RESERVED_FIELDS,
    MutationError,
    NotFound,
    restore_node,
    snapshot_fields,
)
from .overrides import apply_overrides, tombstoned_ids, tombstoned_relationships

logger = logging.getLogger(__name__)


class NothingToRebuild(MutationError):
    """The folded record cannot be put back: there is no row describing it."""


def _check_archived_aggregate(current: dict | None, sources: list[dict]) -> None:
    """Do not roll later enrichment back to an older source snapshot."""
    if current is None:
        return
    expected = Person.model_validate(sources[0])
    for row in sources[1:]:
        _merge_person(expected, Person.model_validate(row))
        if row.get("name_raw") and row["name_raw"] not in expected.name_variants:
            expected.name_variants.append(row["name_raw"])
    before = expected.model_dump(mode="json")
    after = Person.model_validate(current).model_dump(mode="json")
    for field, value in after.items():
        if field in {"processing", "merged_ids"}:
            continue
        original = before[field]
        equal = (all(item in original for item in value) and all(item in value for item in original)
                 if isinstance(value, list) else value == original)
        if not equal:
            raise NothingToRebuild(
                f"{current['id']} has changes in {field} beyond its pre-merge sources; "
                "reconcile the later changes before undoing this fold")
    known = set(expected.model_dump(by_alias=True)) | {"_id", "_version", "groups"}
    for field, value in current.items():
        if field not in known and value != sources[0].get(field):
            raise NothingToRebuild(f"{current['id']} has later changes in {field}; source reconciliation is required")


#: Edges of one person, keyed the way `extract_relationships` keys them.
Edges = dict[tuple[str, str, str, str], list[tuple[str, str, dict]]]


def _prepared_persons(db: Database, ids: list[str]) -> dict[str, dict]:
    """Prepared rows for these people, whichever group holds them.

    `get_rows` looks across groups, so the store's own group is not used
    here (and the panel has no group to name anyway).
    """
    rows = {row["id"]: row for row in PreparedStore(db, group="").get_rows("persons", ids)}
    for person_id, versions in snapshots(db, set(ids) - rows.keys()).items():
        rows[person_id] = versions[0]
    return rows


def rebuildable(db: Database, ids: Iterable[str]) -> set[str]:
    """Which of these people still have a row to be rebuilt from.

    Live rows and durable pre-merge snapshots both support rebuilding.
    """
    return set(_prepared_persons(db, list(ids)))


def _folded_side(client, members: list[str]) -> tuple[str, str, dict]:
    """Which of the two survived the merge, and which one it swallowed.

    Read from the graph rather than taken from the caller: the merge chose
    the survivor by `merge_rank` and wrote that choice nowhere except into
    the survivor's `merged_ids`, which is also what proves this is the fold
    being undone and not some other one.

    Returns:
        (duplicate_id, canonical_id, the survivor's properties).
    """
    if len(set(members)) != 2:
        raise MutationError("a fold is taken apart two records at a time")
    live = {node_id: client.fetch_node_properties("Person", node_id) for node_id in set(members)}
    present = sorted(node_id for node_id, props in live.items() if props is not None)
    if len(present) == 2:
        partial = [
            canonical_id
            for canonical_id in present
            if next(node_id for node_id in present if node_id != canonical_id)
            in (live[canonical_id].get("merged_ids") or [])
        ]
        if len(partial) != 1:
            raise MutationError("both records are in the graph; nothing was folded")
        canonical_id = partial[0]
        duplicate_id = next(node_id for node_id in present if node_id != canonical_id)
        return duplicate_id, canonical_id, live[canonical_id]
    if not present:
        raise NotFound(f"neither {' nor '.join(sorted(set(members)))} is a node any more")
    canonical_id = present[0]
    duplicate_id = next(node_id for node_id in live if node_id != canonical_id)
    canonical = live[canonical_id]
    if duplicate_id not in (canonical.get("merged_ids") or []):
        raise NotFound(f"Person {canonical_id} does not hold {duplicate_id}: "
                       "this is not the fold that happened")
    return duplicate_id, canonical_id, canonical


def _survivor_patch(node: dict, canonical_props: dict | None, duplicate_props: dict,
                    duplicate_id: str, swallowed: list[str]) -> dict:
    """What to set on the survivor to take the duplicate back out of it.

    A fold fills the survivor's empty fields from the duplicate, unions its
    lists and ORs `is_itmo`. The inverse is narrow on purpose: only fields
    the duplicate's row explains are touched, and each goes back to what
    the survivor's own row says, or to None, which removes it. Fields the
    duplicate never had are left alone, so an edit somebody made to one of
    them is not caught in the crossfire.

    Args:
        canonical_props: What the survivor's own row says, or None when it
            has no row left. None means the fields are not touched at all:
            without a row there is no telling which of the survivor's
            values were its own, and clearing them all would take away more
            than the fold ever added.
        swallowed: Ids the duplicate had swallowed before it was swallowed
            itself. They came along with it and leave with it, which is why
            `merged_ids` is handled apart from the other fields.
    """
    patch = {}
    for name in duplicate_props if canonical_props is not None else ():
        if name in RESERVED_FIELDS or name == "merged_ids":
            continue
        restored = canonical_props.get(name)
        if node.get(name) != restored:
            patch[name] = restored
    returning = {duplicate_id, *swallowed}
    kept = [node_id for node_id in (node.get("merged_ids") or []) if node_id not in returning]
    if kept != (node.get("merged_ids") or []):
        patch["merged_ids"] = kept
    return patch


def _give_back(client, db: Database, node_id: str, props: dict, edges: Edges) -> int:
    """Recreate the removed person and the edges their own row declares.

    Edges somebody unlinked by hand are skipped, exactly as a publish skips
    them, otherwise the undo would recreate them and the next
    `apply_overrides` would delete them again, writing a creation and a
    deletion into the journal every time.

    Returns:
        How many edges the graph actually took. Fewer than the row declares
        means the other end is not published, which is the same thing that
        happens on a publish.
    """
    restored = snapshot_fields("Person", props)
    if client.fetch_node_properties("Person", node_id) is None:
        restore_node(client, "Person", node_id, restored)
    else:
        # A prior split may have stopped after recreating the node but before
        # removing the survivor's copied edges. Reapplying its source state is
        # safe and lets the same split finish.
        client.upsert_nodes_batch("Person", [(node_id, restored)])
    dropped = tombstoned_relationships(db)
    given_back = 0
    for (src_label, tgt_label, rel_type, match_field), rels in edges.items():
        kept = [rel for rel in rels
                if (src_label, rel_type, tgt_label, rel[0], rel[1]) not in dropped]
        for chunk in chunked(kept):
            given_back += client.upsert_relationships_batch(
                src_label, tgt_label, rel_type, chunk, match_field)
    return given_back


def _person_identifier_map(row: dict) -> dict[str, ExternalIdentifier]:
    """Stable keys this source row assigned to one person."""
    person = Person.model_validate(row)
    identifiers = {item.normalized_key: item for item in person.identifiers}
    inferred = (
        ("openalex", person.openalex_id or (person.id if person.id.startswith("A") else None)),
        ("orcid", person.orcid),
    )
    for scheme, value in inferred:
        if not value:
            continue
        identifier = ExternalIdentifier(scheme=scheme, value=value)
        identifiers.setdefault(identifier.normalized_key, identifier)
    return identifiers


def _authorship_claims(row: dict, publication_aliases: dict[str, str]) -> set[tuple[str, int]]:
    return {
        (
            publication_aliases.get(authorship["publication_id"], authorship["publication_id"]),
            authorship.get("position") or 0,
        )
        for authorship in row.get("authored") or []
    }


def _source_author_identifier_keys(author: dict) -> set[str]:
    keys = set()
    for raw in author.get("identifiers") or []:
        try:
            keys.add(ExternalIdentifier.model_validate(raw).normalized_key)
        except (TypeError, ValueError):
            continue
    return keys


@dataclass
class _PersonReferenceRestore:
    identity_updates: list[tuple[str, ExternalIdentifier, bool]]
    publication_updates: list[tuple[object, dict]]


def _plan_person_reference_restore(
    db: Database,
    duplicate_id: str,
    canonical_id: str,
    duplicate_row: dict,
    remaining_rows: list[dict],
) -> _PersonReferenceRestore:
    """Reverse the prepared dedup's identity/provenance repointing.

    The operation is idempotent so an interrupted split can repeat it. Keys
    shared by both sides stay on the survivor; only identity facts exclusive
    to the returning source row move back.
    """
    duplicate_identifiers = _person_identifier_map(duplicate_row)
    remaining_identifier_keys = {
        key
        for row in remaining_rows
        for key in _person_identifier_map(row)
    }
    returning_identifiers = {
        key: identifier
        for key, identifier in duplicate_identifiers.items()
        if key not in remaining_identifier_keys
    }

    identity_updates = []
    for key, identifier in returning_identifiers.items():
        document_id = f"person:{key}"
        current = db.identity_keys.find_one({"_id": document_id})
        if current and current.get("entity_id") not in {canonical_id, duplicate_id}:
            raise NothingToRebuild(
                f"identity key {key} is now owned by {current.get('entity_id')}; "
                "the split cannot take it back safely"
            )
        identity_updates.append((document_id, identifier, current is None))

    publication_aliases = {
        alias: row["id"]
        for row in db.publications.find({}, {"id": 1, "merged_ids": 1})
        for alias in [row["id"], *(row.get("merged_ids") or [])]
    }
    duplicate_claims = _authorship_claims(duplicate_row, publication_aliases)
    remaining_claims = {
        claim
        for row in remaining_rows
        for claim in _authorship_claims(row, publication_aliases)
    }
    exclusive_claims = duplicate_claims - remaining_claims
    duplicate_names = {
        " ".join(str(name).casefold().split())
        for name in [duplicate_row.get("name_raw"), *(duplicate_row.get("name_variants") or [])]
        if name
    }
    remaining_names = {
        " ".join(str(name).casefold().split())
        for row in remaining_rows
        for name in [row.get("name_raw"), *(row.get("name_variants") or [])]
        if name
    }
    publication_updates: list[tuple[object, dict]] = []
    for publication in db.publications.find({}):
        publication_id = publication["id"]
        update = {}
        for field in ("source_records", "versions"):
            records = publication.get(field)
            if not records:
                continue
            changed = False
            for record in records:
                for author in record.get("authors") or []:
                    if author.get("person_id") != canonical_id:
                        continue
                    position = author.get("position") or 0
                    author_name = " ".join(str(author.get("name") or "").casefold().split())
                    identity_match = bool(
                        _source_author_identifier_keys(author) & returning_identifiers.keys()
                    )
                    occurrence_match = (publication_id, position) in exclusive_claims
                    name_match = (
                        (publication_id, position) in duplicate_claims
                        and author_name in duplicate_names
                        and author_name not in remaining_names
                    )
                    if identity_match or occurrence_match or name_match:
                        author["person_id"] = duplicate_id
                        changed = True
            if changed:
                update[field] = records
        if update:
            publication_updates.append((publication["_id"], update))

    return _PersonReferenceRestore(identity_updates, publication_updates)


def _apply_person_reference_restore(
    db: Database,
    plan: _PersonReferenceRestore,
    duplicate_id: str,
) -> None:
    for document_id, identifier, missing in plan.identity_updates:
        if missing:
            db.identity_keys.insert_one({
                "_id": document_id,
                "entity": "person",
                "scheme": identifier.scheme,
                "normalized_value": identifier.value,
                "entity_id": duplicate_id,
            })
        else:
            db.identity_keys.update_one(
                {"_id": document_id},
                {"$set": {"entity_id": duplicate_id}},
            )
    for publication_id, update in plan.publication_updates:
        db.publications.update_one({"_id": publication_id}, {"$set": update})


def _take_off(client, canonical_id: str, duplicate_edges: Edges, canonical_edges: Edges) -> int:
    """Remove from the survivor the edges only the duplicate's row claims.

    An edge both rows declare stays: the fold merged the two into one, and
    the survivor had it in its own right. An edge neither row declares is
    left alone too: it was put there by hand, and nothing says by whom or
    onto which of the two.

    Returns:
        How many edges were removed.
    """
    removed = 0
    for key, rels in duplicate_edges.items():
        src_label, tgt_label, rel_type, match_field = key
        mine = {target for _src, target, _props in canonical_edges.get(key, ())}
        theirs = [target for _src, target, _props in rels if target not in mine]
        pairs = [(canonical_id, target) for target in dict.fromkeys(theirs)]
        for chunk in chunked(pairs):
            removed += client.delete_relationships_batch(
                src_label, tgt_label, rel_type, chunk, match_field)
    return removed


def _restore_corresponding(client, canonical_id: str, duplicate_edges: Edges, remaining_edges: Edges) -> None:
    key = ("Person", "Publication", "AUTHORED", "id")
    affected = {target for _src, target, _props in duplicate_edges.get(key, ())}
    flags: dict[str, bool | None] = {}
    for _src, target, props in remaining_edges.get(key, ()):
        if target not in affected:
            continue
        value = props.get("is_corresponding")
        if value is not None:
            flags[target] = bool(flags.get(target) or value)
        else:
            flags.setdefault(target, None)
    rows = [(canonical_id, target, {"is_corresponding": value}) for target, value in flags.items()]
    for chunk in chunked(rows):
        client.set_relationship_properties_batch("Person", "Publication", "AUTHORED", chunk)


def split_person(client, db: Database, members: list[str]) -> dict[str, int | str]:
    """Undo one merge: give the folded person back their node and their work.

    Args:
        client: Graph client; pass the audited one so the rebuild is
            recorded like every other change.
        db: Mongo database, holding the prepared rows this is rebuilt from
            and the manual decisions reapplied at the end.
        members: The two ids the answer was about.

    Returns:
        Which id came back, which one it came out of, and how many edges
        were given back and taken off.

    Raises:
        NotFound: Neither id is a node any more, or the surviving node does
            not name the other among its `merged_ids`.
        NothingToRebuild: No independent source rows or pre-merge snapshots
            remain, or the person was deleted by hand afterwards.
        MutationError: Nothing was folded: both ids are live nodes.
    """
    duplicate_id, canonical_id, canonical = _folded_side(client, members)
    ids = [duplicate_id, canonical_id, *(canonical.get("merged_ids") or [])]
    live = {row["id"]: row for row in PreparedStore(db, group="").get_rows("persons", ids)}
    archives = snapshots(db, ids)
    rows = dict(live)
    used_archive = False
    for person_id, versions in archives.items():
        if person_id not in rows:
            allowed = set(canonical.get("merged_ids") or []) - {person_id}
            compatible = next((row for row in versions if set(row.get("merged_ids") or []) <= allowed), None)
            if compatible is not None:
                rows[person_id] = compatible
            used_archive = True
    duplicate_row = rows.get(duplicate_id)
    if duplicate_row is None:
        raise NothingToRebuild(
            f"{duplicate_id} has no prepared row: nothing to rebuild the record from")
    if duplicate_id in tombstoned_ids(db, "Person"):
        raise NothingToRebuild(
            f"{duplicate_id} was deleted by hand; undoing the fold would only "
            "bring back a record the next publish deletes again")

    duplicate_spec = person_spec(duplicate_row)
    _labels, (node_id, duplicate_props) = extract_node(duplicate_row, duplicate_spec)
    duplicate_edges = extract_relationships(duplicate_row, duplicate_spec)

    returning = {duplicate_id, *(duplicate_row.get("merged_ids") or [])}
    remaining = {canonical_id, *(canonical.get("merged_ids") or [])} - returning
    for person_id in remaining:
        row = rows.get(person_id)
        if row is not None and not {person_id, *(row.get("merged_ids") or [])} <= remaining:
            replacement = next(
                (version for version in archives.get(person_id, [])
                 if {person_id, *(version.get("merged_ids") or [])} <= remaining), None,
            )
            if replacement is not None:
                rows[person_id] = replacement
                used_archive = True
    sources = []
    covered: set[str] = set()
    for person_id in [canonical_id, *sorted(remaining - {canonical_id})]:
        row = rows.get(person_id)
        if row is None or person_id in covered:
            continue
        coverage = {person_id, *(row.get("merged_ids") or [])}
        if not coverage <= remaining:
            raise NothingToRebuild(
                f"{person_id} already aggregates the returning record in prepared data; "
                "independent source rows are required before undoing this fold")
        sources.append(row)
        covered.update(coverage)

    canonical_row = rows.get(canonical_id)
    canonical_props: dict | None = None
    canonical_edges: Edges = {}
    if canonical_row is not None:
        canonical_spec = person_spec(canonical_row)
        _labels, (_id, canonical_props) = extract_node(canonical_row, canonical_spec)
        canonical_edges = extract_relationships(canonical_row, canonical_spec)
    else:
        # No row to restore the fields from, but `merged_ids` must let go.
        logger.warning("Person %s has no prepared row; only its merged_ids is corrected",
                       canonical_id)

    # Undoing one member must not retract facts supplied by another member
    # which remains folded. Missing source rows are not evidence of false.
    complete = remaining <= covered
    if used_archive and not complete:
        raise NothingToRebuild("independent source rows for all remaining people are required to restore prepared data")
    if used_archive:
        _check_archived_aggregate(live.get(canonical_id), [*sources, duplicate_row])
    for row in sources:
        if row["id"] == canonical_id:
            continue
        spec = person_spec(row)
        _labels, (_id, props) = extract_node(row, spec)
        if canonical_props is not None:
            canonical_props.update(_merge_duplicate_properties("Person", canonical_props, props))
        for key, rels in extract_relationships(row, spec).items():
            canonical_edges.setdefault(key, []).extend(rels)
    if not complete:
        canonical_props = None
        logger.warning("Person %s has missing source rows; aggregate fields and edges are retained", canonical_id)

    publication_aliases = client.fetch_merged_id_map("Publication")
    authored_key = ("Person", "Publication", "AUTHORED", "id")
    for edges in (duplicate_edges, canonical_edges):
        if authored_key in edges:
            edges[authored_key] = [
                (src, publication_aliases.get(target, target), props)
                for src, target, props in edges[authored_key]
            ]

    patch = _survivor_patch(canonical, canonical_props, duplicate_props,
                            duplicate_id, duplicate_row.get("merged_ids") or [])
    prepared_updates = []
    if used_archive:
        survivor = Person.model_validate(canonical_row)
        for row in sources:
            if row["id"] != canonical_id:
                _merge_person(survivor, Person.model_validate(row))
                if row.get("name_raw") and row["name_raw"] not in survivor.name_variants:
                    survivor.name_variants.append(row["name_raw"])
        survivor.merged_ids = [identifier for identifier in (canonical.get("merged_ids") or [])
                               if identifier not in returning]
        for row, restored in (
            (canonical_row, survivor.model_dump(mode="json", by_alias=True)),
            (duplicate_row, Person.model_validate(duplicate_row).model_dump(mode="json", by_alias=True)),
        ):
            current = db.persons.find_one({"id": row["id"]}) or {}
            content = {key: value for key, value in row.items() if key not in {"_id", "_version", "groups"}}
            archived_groups = next(
                (version.get("groups") or [] for version in archives.get(row["id"], [])
                 if {key: value for key, value in version.items() if key not in {"_id", "_version", "groups"}} == content),
                [],
            )
            groups = list(dict.fromkeys([
                *(current.get("groups") or []), *(row.get("groups") or []), *archived_groups,
            ]))
            if not groups:
                raise NothingToRebuild(f"{row['id']} has no source group for restoring prepared data")
            prepared_updates.append((groups, content | restored))
    reference_restore = _plan_person_reference_restore(
        db,
        duplicate_id,
        canonical_id,
        duplicate_row,
        sources,
    )
    # Restore Mongo first. If the process stops after this point, a publish
    # can finish rebuilding the graph; the reverse order would leave a live
    # graph node whose source still claims it is folded.
    for groups, row in prepared_updates:
        for group in groups:
            PreparedStore(db, group).upsert_rows("persons", [row])
    _apply_person_reference_restore(db, reference_restore, duplicate_id)
    given_back = _give_back(client, db, node_id, duplicate_props, duplicate_edges)
    taken_off = _take_off(client, canonical_id, duplicate_edges, canonical_edges) if complete else 0
    if complete:
        _restore_corresponding(client, canonical_id, duplicate_edges, canonical_edges)
    if patch:
        # Refresh source-owned fields while retaining the fold marker. If
        # overrides fail below, the next invocation can still recognize and
        # finish this split.
        source_patch = {key: value for key, value in patch.items() if key != "merged_ids"}
        if source_patch:
            client.upsert_nodes_batch("Person", [(canonical_id, source_patch)])
    # As in a publish, rebuilding a node from its source row can bury a
    # hand-made correction, so overrides must precede the commit marker.
    apply_overrides(client, db)
    if patch and "merged_ids" in patch:
        # Removing the folded id commits the graph half of the split. Every
        # earlier operation is idempotent, so an interruption before this
        # write leaves the operation resumable.
        client.upsert_nodes_batch(
            "Person",
            [(canonical_id, {"merged_ids": patch["merged_ids"]})],
        )
    logger.info("split %s back out of %s: %d edge(s) given back, %d taken off",
                duplicate_id, canonical_id, given_back, taken_off)
    return {"duplicate": duplicate_id, "canonical": canonical_id,
            "given_back": given_back, "taken_off": taken_off}
