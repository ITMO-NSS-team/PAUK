"""Shared publication merge-component construction."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set

from pauk.storage import review


def publication_merge_components(
    entity_ids: Iterable[str],
    automatic_pairs: Iterable[tuple[str, str]],
    decisions: Mapping[frozenset[str], str],
    *,
    scope: Set[str] | None = None,
) -> tuple[list[list[str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """Build components without crossing an explicit negative decision.

    Automatic identity edges are applied first. Manual positive edges are
    deterministic and are rejected if their transitive component would
    contain a pair an operator marked as different.
    """
    known = set(entity_ids)
    parent = {entity_id: entity_id for entity_id in known}
    component = {entity_id: {entity_id} for entity_id in known}

    def find(entity_id: str) -> str:
        while parent[entity_id] != entity_id:
            parent[entity_id] = parent[parent[entity_id]]
            entity_id = parent[entity_id]
        return entity_id

    def union(first: str, second: str) -> None:
        first_root, second_root = find(first), find(second)
        if first_root == second_root:
            return
        survivor, removed = sorted((first_root, second_root))
        parent[removed] = survivor
        component[survivor].update(component.pop(removed))

    for first, second in automatic_pairs:
        if first in known and second in known:
            union(first, second)

    manual_different = {
        members
        for members, verdict in decisions.items()
        if verdict == review.DIFFERENT and len(members) == 2
    }
    manual_same = [
        tuple(sorted(members))
        for members, verdict in decisions.items()
        if verdict == review.SAME
        and len(members) == 2
        and members <= known
        and (scope is None or bool(members & scope))
    ]
    accepted = []
    rejected = []
    for first, second in sorted(manual_same):
        combined = component[find(first)] | component[find(second)]
        if any(pair <= combined for pair in manual_different):
            rejected.append((first, second))
            continue
        union(first, second)
        accepted.append((first, second))

    groups = sorted(
        (sorted(members) for members in component.values() if len(members) > 1),
        key=lambda members: tuple(members),
    )
    return groups, accepted, rejected
