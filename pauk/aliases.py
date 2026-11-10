"""Resolve identity aliases consistently in prepared data and the graph."""

from collections import defaultdict, deque


class MergeAliasConflict(ValueError):
    """An ambiguous alias component that must not be folded automatically."""

    def __init__(self, message: str, merges: list[tuple[str, str]], seed: str, label: str | None):
        super().__init__(message)
        self.label = label
        neighbors: dict[str, set[str]] = defaultdict(set)
        for first, second in merges:
            neighbors[first].add(second)
            neighbors[second].add(first)
        members = {seed}
        pending = [seed]
        while pending:
            for neighbor in neighbors[pending.pop()] - members:
                members.add(neighbor)
                pending.append(neighbor)
        self.members = sorted(members)
        self.aliases = sorted({pair for pair in merges if pair[0] in members and pair[0] != pair[1]})


def flatten_merges(merges: list[tuple[str, str]], *, label: str | None = None) -> list[tuple[str, str]]:
    """Resolve aliases to one terminal survivor in dependency order.

    Overlapping historical aliases may name both an intermediate node and
    its survivor. They are safe only when all paths reach the same node.
    Cycles and competing survivors are rejected without choosing a winner.
    """
    targets: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, set[str]] = defaultdict(set)
    for duplicate, canonical in merges:
        if duplicate != canonical:
            targets[duplicate].add(canonical)
            parents[canonical].add(duplicate)
    remaining = {node: len(destinations) for node, destinations in targets.items()}
    roots = {node: node for node in parents if node not in targets}
    depth: dict[str, int] = defaultdict(int)
    ready = deque(roots)
    while ready:
        node = ready.popleft()
        for parent in parents.get(node, ()):
            depth[parent] = max(depth[parent], depth[node] + 1)
            root = roots.setdefault(parent, roots[node])
            if root != roots[node]:
                raise MergeAliasConflict(f"Conflicting merge targets for {parent}", merges, parent, label)
            remaining[parent] -= 1
            if remaining[parent] == 0:
                ready.append(parent)
    if unresolved := sorted(node for node, count in remaining.items() if count):
        raise MergeAliasConflict("Cyclic merge aliases cannot be applied", merges, unresolved[0], label)
    # A nearer survivor's scalar values take precedence over the records it had swallowed.
    return [(node, roots[node]) for node in sorted(targets, key=depth.__getitem__)]
