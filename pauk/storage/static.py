from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from pauk.models import Department, Organization


def _identifier(prefix: str, name: str) -> str:
    """Deterministic id derived from a name, so graph links stay stable across runs."""
    return f"{prefix}_{sha256(name.casefold().encode()).hexdigest()[:12]}"


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
        entries = self._catalog_entries()
        kind_by_name = {(e.get("name_en") or "").strip(): (e.get("kind") or "").strip() for e in entries}
        departments: list[Department] = []
        for entry in entries:
            name_en = (entry.get("name_en") or "").strip()
            if not name_en or (entry.get("kind") or "").strip() == "organization":
                continue
            parent = (entry.get("parent") or "").strip()
            # A unit is PART_OF its parent: the Organisation if the parent is one,
            # otherwise the parent Department. At most one link id is set.
            if parent and kind_by_name.get(parent) == "organization":
                parent_id, organization_id = None, _identifier("org", parent)
            else:
                parent_id, organization_id = (_identifier("dept", parent) if parent else None), None
            departments.append(
                Department(
                    id=_identifier("dept", name_en),
                    name_en=name_en,
                    name_ru=(entry.get("name_ru") or "").strip() or None,
                    name_variants=entry.get("aliases") or [],
                    context_aliases=entry.get("context_aliases") or [],
                    parent_id=parent_id,
                    organization_id=organization_id,
                    kind=(entry.get("kind") or "").strip() or None,
                )
            )
        return departments

    def organizations(self) -> list[Organization]:
        """Root organisations from the catalogue (kind=="organization").

        Departments link to these via organization_id. Empty when the catalogue is
        overridden by a departments.jsonl (that carries no organisation rows).
        """
        if (self.root / "departments.jsonl").exists():
            return []
        if not (self.root / "departments_catalog.json").exists():
            return []
        organizations: list[Organization] = []
        for entry in self._catalog_entries():
            if (entry.get("kind") or "").strip() != "organization":
                continue
            name_en = (entry.get("name_en") or "").strip()
            if not name_en:
                continue
            organizations.append(
                Organization(
                    id=_identifier("org", name_en),
                    name_en=name_en,
                    name_ru=(entry.get("name_ru") or "").strip() or None,
                    ror_id=(entry.get("ror_id") or "").strip() or None,
                    country=(entry.get("country") or "").strip() or None,
                    type=(entry.get("type") or "").strip() or None,
                )
            )
        return organizations

    def _catalog_entries(self) -> list[dict]:
        catalog = self.root / "departments_catalog.json"
        if not catalog.exists():
            path = self.root / "departments.jsonl"
            raise FileNotFoundError(f"department catalogue is missing; expected {path} or {catalog}")
        return json.loads(catalog.read_text(encoding="utf-8-sig"))
