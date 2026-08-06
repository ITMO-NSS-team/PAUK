from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from pauk.models import Department, School


def _identifier(prefix: str, name: str) -> str:
    """Deterministic id derived from a name, so graph links stay stable across runs."""
    return f"{prefix}_{sha256(name.casefold().encode()).hexdigest()[:12]}"


def _school_id(school_en: str | None) -> str | None:
    school_en = (school_en or "").strip()
    return _identifier("school", school_en) if school_en else None


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
        departments: list[Department] = []
        for entry in self._catalog_entries():
            name_en = (entry.get("name_en") or "").strip()
            if not name_en:
                continue
            departments.append(
                Department(
                    id=_identifier("dept", name_en),
                    name_en=name_en,
                    name_ru=(entry.get("name_ru") or "").strip() or None,
                    name_variants=entry.get("aliases") or [],
                    school_id=_school_id(entry.get("school_en")),
                )
            )
        return departments

    def schools(self) -> list[School]:
        """Distinct top-level schools from the catalogue (School graph nodes).

        The hierarchy comes from each entry's school_en/school_ru; School to
        Department is already many-to-one. Empty when there is no catalogue.
        """
        # departments.jsonl is a full override of the catalogue and carries only
        # school_id (no school names), so in that mode there is no school layer to
        # emit; keep departments() and schools() on the same source.
        if (self.root / "departments.jsonl").exists():
            return []
        if not (self.root / "departments_catalog.json").exists():
            return []
        seen: set[str] = set()
        schools: list[School] = []
        for entry in self._catalog_entries():
            name_en = (entry.get("school_en") or "").strip()
            if not name_en:
                continue
            identifier = _identifier("school", name_en)
            if identifier in seen:
                continue
            seen.add(identifier)
            schools.append(
                School(
                    id=identifier,
                    name_en=name_en,
                    name_ru=(entry.get("school_ru") or "").strip() or None,
                )
            )
        return schools

    def _catalog_entries(self) -> list[dict]:
        catalog = self.root / "departments_catalog.json"
        if not catalog.exists():
            path = self.root / "departments.jsonl"
            raise FileNotFoundError(f"department catalogue is missing; expected {path} or {catalog}")
        return json.loads(catalog.read_text(encoding="utf-8-sig"))
