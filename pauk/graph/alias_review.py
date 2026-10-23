"""Expose unsafe historical person aliases without guessing a survivor."""

from pymongo.database import Database

from pauk.storage import review

from .client import MergeAliasConflict


def record_alias_conflict(db: Database, conflict: MergeAliasConflict, operation: str) -> None:
    if conflict.label != "Person":
        return
    review.record_held(
        db,
        [{
            "status": "held",
            "kind": review.ALIAS,
            "persons": conflict.members,
            "route": "merge_alias_conflict",
            "reason": str(conflict),
            "held_because": [str(conflict)],
            "alias_edges": [{"alias": alias, "target": target} for alias, target in conflict.aliases],
            "operation": operation,
            "requires_alias_repair": True,
        }],
        source=review.GRAPH,
    )
