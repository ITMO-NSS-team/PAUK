"""Plan a targeted rerun for completed people with invalid split names.

Reads MongoDB and writes a manifest plus one person-id file per selected
group. It never changes MongoDB. Each global Person is assigned to exactly
one group, using a deterministic greedy cover so the repair does not pay for
the same LLM request through every group that references the person.

Run the printed `pauk enrich author_names ... --force` commands only after
reviewing the manifest and taking a prepared-data snapshot.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pauk.pipeline.stages.author_names import (
    REQUIRED_NAME_FIELDS,
    required_name_field_issues,
)
from pauk.settings import settings
from pauk.storage import PreparedStore
from pauk.storage.mongo import get_mongo_client
from pauk.storage.naming import validate_group


def valid_groups(person: dict) -> list[str]:
    groups = []
    for group in person.get("groups") or []:
        if not isinstance(group, str):
            continue
        try:
            groups.append(validate_group(group))
        except ValueError:
            continue
    return sorted(set(groups))


def assign_repair_groups(people: list[dict]) -> tuple[dict[str, list[str]], list[str]]:
    """Assign every reachable person once while keeping the group count low."""
    groups_by_person = {person["id"]: valid_groups(person) for person in people}
    members_by_group: dict[str, set[str]] = {}
    for person_id, groups in groups_by_person.items():
        for group in groups:
            members_by_group.setdefault(group, set()).add(person_id)

    remaining = {person["id"] for person in people if groups_by_person[person["id"]]}
    assignments: dict[str, list[str]] = {}
    while remaining:
        group = min(
            members_by_group,
            key=lambda candidate: (-len(members_by_group[candidate] & remaining), candidate),
        )
        assigned = sorted(members_by_group[group] & remaining)
        assignments[group] = assigned
        remaining.difference_update(assigned)

    unresolved = sorted(
        person["id"] for person in people if not groups_by_person[person["id"]]
    )
    return assignments, unresolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/reports/author-names-repair"),
        help="directory for the manifest and per-group id files",
    )
    args = parser.parse_args()

    client = get_mongo_client(settings)
    try:
        db = client[settings.mongo_db]
        projection = {
            "id": True,
            "name_raw": True,
            "is_itmo": True,
            "groups": True,
            **dict.fromkeys(REQUIRED_NAME_FIELDS, True),
        }
        cursor = db[PreparedStore.COLLECTIONS["persons"]].find(
            {"_processing.author_names.status": "completed"}, projection
        )
        people = []
        for person in cursor:
            field_issues = required_name_field_issues(person)
            if not field_issues:
                continue
            people.append(
                {
                    "id": person.get("id") or str(person["_id"]),
                    "name_raw": person.get("name_raw"),
                    "is_itmo": bool(person.get("is_itmo")),
                    "groups": person.get("groups") or [],
                    "field_issues": field_issues,
                }
            )
        host = client.address[0] if client.address else None
    finally:
        client.close()

    people.sort(key=lambda person: person["id"])
    assignments, unresolved = assign_repair_groups(people)
    selected_group_by_person = {
        person_id: group
        for group, person_ids in assignments.items()
        for person_id in person_ids
    }
    for person in people:
        person["selected_group"] = selected_group_by_person.get(person["id"])

    args.out.mkdir(parents=True, exist_ok=True)
    command_rows = []
    for group, person_ids in sorted(assignments.items()):
        ids_path = args.out / f"{group}.txt"
        ids_path.write_text("\n".join(person_ids) + "\n", encoding="utf-8")
        command_rows.append(
            {
                "group": group,
                "ids_file": str(ids_path),
                "people": len(person_ids),
                "command": (
                    f'uv run pauk enrich author_names --group {group} '
                    f'--input "{ids_path}" --entity persons --force'
                ),
            }
        )

    invalid_counts = Counter(
        field for person in people for field in person["field_issues"]
    )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "database": settings.mongo_db,
        "host": host,
        "people": len(people),
        "itmo_people": sum(person["is_itmo"] for person in people),
        "invalid_by_field": dict(sorted(invalid_counts.items())),
        "unresolved_without_group": unresolved,
        "commands": command_rows,
        "records": people,
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"invalid completed people: {len(people)}")
    print(f"  ITMO people:             {manifest['itmo_people']}")
    for field, count in invalid_counts.most_common():
        print(f"  invalid {field:<15} {count}")
    print(f"repair groups: {len(assignments)}")
    print(f"without a valid group: {len(unresolved)}")
    print(f"manifest: {manifest_path}")
    if command_rows:
        print("\nCommands to review and run sequentially after taking a snapshot:")
        for row in command_rows:
            print(row["command"])
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
