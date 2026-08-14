from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path

from pauk.models import Department, Organization


class StaticStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def departments(self) -> list[Department]:
        path = self.root / "departments.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                return [Department.model_validate(json.loads(line)) for line in fh if line.strip()]

        # The catalogue is the versioned static source. Each entry carries a
        # human-readable `uid` used directly as the graph node id and referenced
        # by `parent`, so a unit is never repeated and stays cheap to rename.
        entries = self._catalog_entries
        kind_by_uid = {(e.get("uid") or "").strip(): (e.get("kind") or "").strip() for e in entries}
        departments: list[Department] = []
        for entry in entries:
            uid = (entry.get("uid") or "").strip()
            name_en = (entry.get("name_en") or "").strip()
            if not uid or not name_en or (entry.get("kind") or "").strip() == "organization":
                continue
            parent = (entry.get("parent") or "").strip()
            if parent and parent not in kind_by_uid:
                # A typo here would otherwise become a silently orphaned unit (its
                # PART_OF edge drops at load time); fail loudly instead.
                raise ValueError(f"department {uid!r} has unknown parent uid {parent!r}")
            # A unit is PART_OF its parent (referenced by uid): the Organisation if
            # the parent is one, otherwise the parent Department. One link id at most.
            if parent and kind_by_uid.get(parent) == "organization":
                parent_id, organization_id = None, parent
            else:
                parent_id, organization_id = (parent or None), None
            departments.append(
                Department(
                    id=uid,
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
        for entry in self._catalog_entries:
            if (entry.get("kind") or "").strip() != "organization":
                continue
            uid = (entry.get("uid") or "").strip()
            name_en = (entry.get("name_en") or "").strip()
            if not uid or not name_en:
                continue
            organizations.append(
                Organization(
                    id=uid,
                    name_en=name_en,
                    name_ru=(entry.get("name_ru") or "").strip() or None,
                    ror_id=(entry.get("ror_id") or "").strip() or None,
                    country=(entry.get("country") or "").strip() or None,
                    type=(entry.get("type") or "").strip() or None,
                )
            )
        return organizations

    @cached_property
    def _catalog_entries(self) -> list[dict]:
        # Parsed once per store: departments() and organizations() both read it in
        # the same run, and the file never changes over a store's lifetime.
        catalog = self.root / "departments_catalog.json"
        if not catalog.exists():
            path = self.root / "departments.jsonl"
            raise FileNotFoundError(f"department catalogue is missing; expected {path} or {catalog}")
        return json.loads(catalog.read_text(encoding="utf-8-sig"))
