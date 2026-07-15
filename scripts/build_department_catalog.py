"""Одноразовый генератор официального en↔ru каталога департаментов ИТМО.

Скрейпит официальную страницу структуры ИТМО (EN) — извлекает факультеты/институты
с их числовым faculty_id, slug и английским названием, группирует по мегафакультетам
(schools) в порядке документа, затем по тому же faculty_id тянет русское название с
RU-сайта (itmo.ru/ru/viewfaculty/<id>/<slug>). Результат — draft JSON.

НЕ входит в пайплайн. Результат ОБЯЗАТЕЛЬНО сверяется вручную и коммитится как
data/departments_catalog.json — источник истины для seed_departments.py.

Запуск из корня проекта:
    uv run python scripts/build_department_catalog.py
    uv run python scripts/build_department_catalog.py --out data/departments_catalog.draft.json
"""

import argparse
import json
import logging
import re
import time
from pathlib import Path

import requests

from config import (
    DEPARTMENTS_CATALOG_PATH,
    DOWNLOAD_TIMEOUT,
    ITMO_SITE_RU,
    ITMO_STRUCTURE_URL_EN,
    USER_AGENT,
)

logger = logging.getLogger(__name__)

# Ссылка на страницу факультета: /en/faculty/<id>/<slug>.htm + текст ссылки.
FACULTY_LINK_RE = re.compile(
    r'/en/faculty/(\d+)/([^"\'>]+?\.htm)"[^>]*>\s*([^<]+?)\s*<', re.IGNORECASE
)
TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
# Хвост " — Университет ИТМО" / " | …": только em-dash или pipe с ведущим пробелом,
# чтобы не обрезать названия с обычным дефисом (напр. "информационно-навигационных").
TITLE_SUFFIX_RE = re.compile(r"\s+[—|].*$")


def fetch(url: str) -> str | None:
    """GET с полит-агентом; None при ошибке (redirects следуются автоматически)."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Не удалось получить %s: %s", url, exc)
        return None


def parse_en_structure(html: str) -> list[dict]:
    """Факультеты в порядке документа: {faculty_id, slug, name_en, is_school}."""
    rows: list[dict] = []
    seen: set[str] = set()
    for fid, slug, name_en in FACULTY_LINK_RE.findall(html):
        if fid in seen:
            continue
        seen.add(fid)
        rows.append(
            {
                "faculty_id": int(fid),
                "slug": slug,
                "name_en": name_en.strip(),
                "is_school": "megafakultet" in slug or "megafakultet" in name_en.lower(),
            }
        )
    return rows


def assign_schools(rows: list[dict]) -> None:
    """Проставляет school_en каждому факультету по последнему виденному мегафакультету."""
    current = ""
    for row in rows:
        if row["is_school"]:
            current = row["name_en"]
            row["school_en"] = row["name_en"]
        else:
            row["school_en"] = current


def fetch_name_ru(faculty_id: int, slug: str) -> str | None:
    """Русское название подразделения из <title> RU-страницы факультета."""
    url = f"{ITMO_SITE_RU}/ru/viewfaculty/{faculty_id}/{slug}"
    html = fetch(url)
    if not html:
        return None
    m = TITLE_RE.search(html)
    if not m:
        return None
    return TITLE_SUFFIX_RE.sub("", m.group(1)).strip() or None


def build_catalog() -> list[dict]:
    """Собирает каталог: EN-структура + RU-названия по faculty_id."""
    html = fetch(ITMO_STRUCTURE_URL_EN)
    if not html:
        raise SystemExit("Не удалось получить страницу структуры ИТМО.")
    rows = parse_en_structure(html)
    assign_schools(rows)
    logger.info("Найдено %d подразделений на EN-странице структуры.", len(rows))

    catalog: list[dict] = []
    for row in rows:
        name_ru = fetch_name_ru(row["faculty_id"], row["slug"])
        if not name_ru:
            logger.warning("Нет name_ru для #%s (%s) — дозаполнить вручную.",
                           row["faculty_id"], row["name_en"])
        catalog.append(
            {
                "faculty_id": row["faculty_id"],
                "school_en": row["school_en"],
                "school_ru": "",  # дозаполнить вручную при сверке
                "name_en": row["name_en"],
                "name_ru": name_ru or "",
                "aliases": [],
            }
        )
        time.sleep(0.3)
    catalog.sort(key=lambda d: d["name_en"].lower())
    return catalog


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Сгенерировать draft официального каталога департаментов ИТМО.")
    parser.add_argument("--out", type=Path, default=DEPARTMENTS_CATALOG_PATH,
                        help="Куда писать JSON (по умолчанию — путь каталога из config).")
    args = parser.parse_args()

    catalog = build_catalog()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Записано %d записей в %s. СВЕРЬТЕ ВРУЧНУЮ перед коммитом.", len(catalog), args.out)


if __name__ == "__main__":
    main()
