from __future__ import annotations

from pathlib import Path

import json
from hashlib import sha256

from pauk.models import Department


class StaticStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def departments(self) -> list[Department]:
        path = self.root / "departments.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                return [Department.model_validate(json.loads(line)) for line in fh if line.strip()]

        # The historical ITMO catalogue is retained as a versioned static
        # source.  IDs are deterministically derived from the official name,
        # so graph links remain stable across runs without SQLite.
        catalog = self.root / "departments_catalog.json"
        if not catalog.exists():
            raise FileNotFoundError(
                f"department catalogue is missing; expected {path} or {catalog}"
            )
        entries = json.loads(catalog.read_text(encoding="utf-8-sig"))
        departments: list[Department] = []
        for entry in entries:
            name_en = (entry.get("name_en") or "").strip()
            if not name_en:
                continue
            identifier = sha256(name_en.casefold().encode()).hexdigest()[:12]
            departments.append(Department(
                id=f"dept_{identifier}", name_en=name_en,
                name_ru=(entry.get("name_ru") or "").strip() or None,
                name_variants=entry.get("aliases") or [],
            ))
        return departments
