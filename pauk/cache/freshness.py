from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def is_fresh(path: Path, max_age: timedelta) -> bool:
    if not path.is_file():
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - modified <= max_age
