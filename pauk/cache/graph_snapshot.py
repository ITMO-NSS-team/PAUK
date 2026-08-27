from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pauk.storage import AtomicWriter

SCHEMA_VERSION = 2


def write_snapshot(path: Path, graph: dict[str, list]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "graph": graph,
    }
    with AtomicWriter(path) as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))


def read_snapshot(path: Path) -> dict[str, list]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported graph snapshot schema: {payload.get('schema_version')}")
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("graph snapshot has no graph object")
    return graph
