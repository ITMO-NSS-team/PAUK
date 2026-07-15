"""Заливка официального en↔ru каталога департаментов в таблицу departments.

Шаг пайплайна ПЕРЕД enrich_departments. Идемпотентно (upsert по UNIQUE name_en):
пишет авторитетный name_ru и добавляет aliases в name_variants. Схему БД не меняет.
«Официальность» депа определяется членством name_en в каталоге в рантайме (отдельной
колонки нет). Запуск: uv run python scripts/seed_departments.py
"""

import argparse
import json
import logging
import sqlite3
import uuid

from catalog import load_catalog, normalize
from config import DB_PATH

logger = logging.getLogger(__name__)


def _merge_variants(existing_raw: str | None, aliases: list[str], name_en: str) -> list[str]:
    """Объединяет существующие варианты с aliases из каталога (без дублей и без name_en)."""
    try:
        existing = json.loads(existing_raw) if existing_raw else []
        if not isinstance(existing, list):
            existing = []
    except (TypeError, ValueError):
        existing = []
    seen = {normalize(v) for v in existing}
    seen.add(normalize(name_en))
    out = list(existing)
    for alias in aliases:
        norm = normalize(alias)
        if alias and norm and norm not in seen:
            out.append(alias)
            seen.add(norm)
    return out


def seed(conn: sqlite3.Connection) -> tuple[int, int]:
    """Upsert каждой записи каталога в departments. Возвращает (вставлено, обновлено)."""
    cur = conn.cursor()
    catalog = load_catalog()
    if not catalog:
        logger.warning("Каталог пуст или не найден — нечего заливать.")
        return 0, 0

    inserted = updated = 0
    for entry in catalog:
        name_en = (entry.get("name_en") or "").strip()
        if not name_en:
            continue
        name_ru = (entry.get("name_ru") or "").strip() or None
        aliases = entry.get("aliases", []) or []

        row = cur.execute(
            "SELECT id, name_variants FROM departments WHERE name_en = ?", (name_en,)
        ).fetchone()
        if row:
            dept_id, existing_raw = row
            variants = _merge_variants(existing_raw, aliases, name_en)
            cur.execute(
                "UPDATE departments SET name_ru = COALESCE(?, name_ru), name_variants = ? WHERE id = ?",
                (name_ru, json.dumps(variants, ensure_ascii=False), dept_id),
            )
            updated += 1
        else:
            dept_id = f"dept_{uuid.uuid4().hex[:12]}"
            variants = _merge_variants(None, aliases, name_en)
            cur.execute(
                "INSERT INTO departments (id, name_ru, name_en, name_variants) VALUES (?, ?, ?, ?)",
                (dept_id, name_ru, name_en, json.dumps(variants, ensure_ascii=False)),
            )
            inserted += 1
    conn.commit()
    return inserted, updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argparse.ArgumentParser(description="Залить официальный каталог департаментов в БД.").parse_args()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        inserted, updated = seed(conn)
        logger.info("Каталог залит: вставлено %d, обновлено %d официальных департаментов.", inserted, updated)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
