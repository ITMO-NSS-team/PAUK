"""Taking a fold apart: the person a merge removed, put back.

`merge_nodes` moves the duplicate's relationships onto the survivor and
deletes it, and the graph keeps no record of which edge came from where.
The prepared rows do. A person's edges are declared by that person's own
row and by nothing else — no spec in the registry points *at* a Person —
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

from pymongo.database import Database

from pauk.storage.prepared import PreparedStore

from .client import chunked
from .extract import extract_node, extract_relationships, person_spec
from .mutations import RESERVED_FIELDS, MutationError, NotFound, create_node
from .overrides import apply_overrides, tombstoned_ids, tombstoned_relationships

logger = logging.getLogger(__name__)


class NothingToRebuild(MutationError):
    """The folded record cannot be put back: there is no row describing it."""


#: Edges of one person, keyed the way `extract_relationships` keys them:
#: (src_label, tgt_label, rel_type, tgt_match_field) -> (src_id, tgt_id, props).
Edges = dict[tuple[str, str, str, str], list[tuple[str, str, dict]]]


def _prepared_persons(db: Database, ids: list[str]) -> dict[str, dict]:
    """Prepared rows for these people, whichever group holds them.

    `get_rows` looks across groups, so the store's own group is not used
    here — and the panel has no group to name anyway.
    """
    return {row["id"]: row for row in PreparedStore(db, group="").get_rows("persons", ids)}


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
    """What to set on the survivor to take the duplicate back out of it.

    A fold fills the survivor's empty fields from the duplicate, unions its
    lists and ORs `is_itmo`. The inverse is narrow on purpose: only fields
    the duplicate's row explains are touched, and each goes back to what
    the survivor's own row says — or to None, which removes it. Fields the
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
    them — otherwise the undo would recreate them and the next
    `apply_overrides` would delete them again, writing a creation and a
    deletion into the journal every time.

    Returns:
        How many edges the graph actually took. Fewer than the row declares
        means the other end is not published, which is the same thing that
        happens on a publish.
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
    """Remove from the survivor the edges only the duplicate's row claims.

    An edge both rows declare stays: the fold merged the two into one, and
    the survivor had it in its own right. An edge neither row declares is
    left alone too — it was put there by hand, and nothing says by whom or
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
        NothingToRebuild: The folded person has no prepared row left, or
            was deleted by hand afterwards. The collection stage deletes
            the rows it folds, and there is no other copy of what the node
            held.
        MutationError: Nothing was folded — both ids are live nodes.
    """
    duplicate_id, canonical_id, canonical = _folded_side(client, members)
    rows = _prepared_persons(db, [duplicate_id, canonical_id])
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

    canonical_row = rows.get(canonical_id)
    canonical_props: dict | None = None
    canonical_edges: Edges = {}
    if canonical_row is not None:
        canonical_spec = person_spec(canonical_row)
        _labels, (_id, canonical_props) = extract_node(canonical_row, canonical_spec)
        canonical_edges = extract_relationships(canonical_row, canonical_spec)
    else:
        # Its own row is gone (folded away by a later run), so there is
        # nothing to restore the survivor's fields from. Its `merged_ids`
        # still has to let the duplicate go, or the next publish folds the
        # pair straight back.
        logger.warning("Person %s has no prepared row; only its merged_ids is corrected",
                       canonical_id)

    patch = _survivor_patch(canonical, canonical_props, duplicate_props,
                            duplicate_id, duplicate_row.get("merged_ids") or [])
    if patch:
        # The plain upsert, not the person one: `is_itmo` is sticky there
        # and never goes back down, and a survivor made ITMO by the record
        # it swallowed has to stop being it.
        client.upsert_nodes_batch("Person", [(canonical_id, patch)])
    given_back = _give_back(client, db, node_id, duplicate_props, duplicate_edges)
    taken_off = _take_off(client, canonical_id, duplicate_edges, canonical_edges)
    # Last, as in a publish: rebuilding a node from its source row is the
    # one thing that can bury a hand-made correction under what the source
    # says.
    apply_overrides(client, db)
    logger.info("split %s back out of %s: %d edge(s) given back, %d taken off",
                duplicate_id, canonical_id, given_back, taken_off)
    return {"duplicate": duplicate_id, "canonical": canonical_id,
            "given_back": given_back, "taken_off": taken_off}
