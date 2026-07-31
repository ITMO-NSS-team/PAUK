from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class RawStore:
    """Append-only storage for complete external-source responses."""

    def __init__(self, root: Path, group: str) -> None:
        self.group_dir = root / group

    def path_for(self, source: str) -> Path:
        return self.group_dir / f"{source}.jsonl"

    def append(self, source: str, payload: dict[str, Any], request: dict[str, Any]) -> None:
        path = self.path_for(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "source": source,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "request": request,
            "payload": payload,
        }
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read(self, source: str) -> Iterator[dict[str, Any]]:
        path = self.path_for(source)
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    logger.warning("ignoring incomplete final raw row in %s", path)
                    return
                raise
