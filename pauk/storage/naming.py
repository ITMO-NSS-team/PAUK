from __future__ import annotations

from datetime import date
from pathlib import Path
import re


_GROUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def group_name(*, work_id: str | None = None, date_from: str | None = None,
               date_to: str | None = None, name: str | None = None) -> str:
    if name:
        return name
    today = date.today().isoformat()
    if work_id:
        return f"{today}__{work_id}"
    if date_from and date_to:
        return f"{today}__from_{date_from}__to_{date_to}"
    raise ValueError("provide a work ID, a date range, or a group name")


def ensure_new(root: Path, group: str) -> None:
    if (root / group).exists():
        raise FileExistsError(f"group already exists: {root / group}")


def validate_group(group: str) -> str:
    if not _GROUP_NAME.fullmatch(group):
        raise ValueError("group name may contain only letters, digits, '.', '_' and '-'")
    return group
