"""Undo a Person fold using prepared rows or durable pre-merge snapshots.

The graph loses edge provenance during a fold, so reconstruction uses the
loader's extractors and the source rows. Registry-defined Person edges are
outgoing and can be recovered from those rows. Edges attributable to neither
source stay on the survivor. Manual overrides are reapplied after reconstruction.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from pymongo.database import Database

from pauk.models import Person
from pauk.pipeline.normalize import _merge_person
from pauk.storage.person_merge_sources import snapshots
from pauk.storage.prepared import PreparedStore

from .client import _merge_duplicate_properties, chunked
from .extract import extract_node, extract_relationships, person_spec
from .mutations import RESERVED_FIELDS, MutationError, NotFound, create_node
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
    """Read source rows across groups, falling back to pre-merge snapshots."""
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
    """Verify the fold against the survivor's merged_ids, ignoring caller order.

    Returns:
        (duplicate_id, canonical_id, survivor properties).
    """
    if len(set(members)) != 2:
        raise MutationError("a fold is taken apart two records at a time")
    live = {node_id: client.fetch_node_properties("Person", node_id) for node_id in set(members)}
    present = sorted(node_id for node_id, props in live.items() if props is not None)
    if len(present) == 2:
        raise MutationError("both records are in the graph; nothing was folded")
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
    """Restore survivor fields affected by the duplicate, leaving other fields intact.

    Args:
        canonical_props: Survivor source properties. If absent, preserve field
            values because their origin cannot be established.
        swallowed: Aliases brought in by the duplicate, which leave with it.
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
    """Recreate the person and source-declared edges, excluding manual tombstones.

    Returns the number of edges restored; missing targets are skipped as in publishing.
    """
    create_node(client, "Person", node_id,
                {name: value for name, value in props.items() if name not in RESERVED_FIELDS})
    dropped = tombstoned_relationships(db)
    given_back = 0
    for (src_label, tgt_label, rel_type, match_field), rels in edges.items():
        kept = [rel for rel in rels
                if (src_label, rel_type, tgt_label, rel[0], rel[1]) not in dropped]
        for chunk in chunked(kept):
            given_back += client.upsert_relationships_batch(
                src_label, tgt_label, rel_type, chunk, match_field)
    return given_back


def _take_off(client, canonical_id: str, duplicate_edges: Edges, canonical_edges: Edges) -> int:
    """Remove edges declared only by the duplicate and return their count.

    Edges declared by both sources or neither source stay on the survivor.
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
    if patch:
        # The plain upsert: `is_itmo` is sticky in the person one.
        client.upsert_nodes_batch("Person", [(canonical_id, patch)])
    given_back = _give_back(client, db, node_id, duplicate_props, duplicate_edges)
    taken_off = _take_off(client, canonical_id, duplicate_edges, canonical_edges) if complete else 0
    if complete:
        _restore_corresponding(client, canonical_id, duplicate_edges, canonical_edges)
    # Publishing must see the split too, otherwise the surviving aggregate
    # would silently fold the restored person again on the next publish.
    for groups, row in prepared_updates:
        for group in groups:
            PreparedStore(db, group).upsert_rows("persons", [row])
    # Last, as in a publish: rebuilding a node from its source row is the
    # one thing that can bury a hand-made correction under what the source
    # says.
    apply_overrides(client, db)
    logger.info("split %s back out of %s: %d edge(s) given back, %d taken off",
                duplicate_id, canonical_id, given_back, taken_off)
    return {"duplicate": duplicate_id, "canonical": canonical_id,
            "given_back": given_back, "taken_off": taken_off}
