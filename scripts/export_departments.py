"""Экспорт списка департаментов en↔ru для валидации переводов.

Выгружает departments в reports/departments_en_ru.csv: name_en, name_ru, признак
официальности (name_en ∈ каталог), число вариантов и usage_count (сколько публикаций
связано через publication_departments). Список удобно глазами проверить / провалидировать
LLM. Read-only по данным. Запуск: uv run python scripts/export_departments.py
"""

import argparse
import csv
import json
import logging
import sqlite3
from pathlib import Path

from catalog import load_catalog, official_name_en_set
from config import DB_PATH, ROOT_DIR

logger = logging.getLogger(__name__)

DEFAULT_OUT = ROOT_DIR / "reports" / "departments_en_ru.csv"


def _n_variants(raw: str | None) -> int:
    try:
        v = json.loads(raw) if raw else []
        return len(v) if isinstance(v, list) else 0
    except (TypeError, ValueError):
        return 0


def collect_rows(conn: sqlite3.Connection, official: set[str]) -> list[dict]:
    """Строки экспорта, отсортированные: официальные первыми, затем по usage_count."""
    cur = conn.cursor()
    usage = dict(
        cur.execute(
            "SELECT department_id, COUNT(*) FROM publication_departments GROUP BY department_id"
        ).fetchall()
    )
    rows = []
    for dept_id, name_en, name_ru, variants in cur.execute(
        "SELECT id, name_en, name_ru, name_variants FROM departments"
    ):
        rows.append(
            {
                "name_en": name_en,
                "name_ru": name_ru or "",
                "official": "yes" if name_en in official else "no",
                "n_variants": _n_variants(variants),
                "usage_count": usage.get(dept_id, 0),
            }
        )
    rows.sort(key=lambda r: (r["official"] != "yes", -r["usage_count"], r["name_en"].lower()))
    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Экспорт департаментов en↔ru в CSV для валидации.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Путь CSV.")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        official = official_name_en_set(load_catalog())
        rows = collect_rows(conn, official)
    finally:
        conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["name_en", "name_ru", "official", "n_variants", "usage_count"])
        writer.writeheader()
        writer.writerows(rows)

    n_official = sum(1 for r in rows if r["official"] == "yes")
    logger.info("Выгружено %d департаментов (%d официальных) в %s", len(rows), n_official, args.out)


if __name__ == "__main__":
    main()
