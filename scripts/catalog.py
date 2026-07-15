"""Официальный каталог департаментов ИТМО — загрузка и индексация.

Общий хелпер: читает data/departments_catalog.json (source of truth) и строит
нормализованный индекс для точного (stage-1) матчинга. Используется
seed_departments.py, enrich_departments.py и export_departments.py. Чистые функции,
независимые от SQLite — переживут миграцию БД.
"""

import json
import re
from pathlib import Path

from config import DEPARTMENTS_CATALOG_PATH

_QUOTES_RE = re.compile(r'["“”‘’«»]')
_NON_ALNUM_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


def normalize(s: str) -> str:
    """Нормализует название для сравнения: без кавычек/пунктуации, lower, схлопнутые пробелы."""
    if not s:
        return ""
    s = _QUOTES_RE.sub("", s).lower().replace("&", " and ")
    s = _NON_ALNUM_RE.sub(" ", s)
    return " ".join(s.split())


def load_catalog(path: Path = DEPARTMENTS_CATALOG_PATH) -> list[dict]:
    """Официальный каталог как список записей. Пустой список, если файла нет."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def official_name_en_set(catalog: list[dict]) -> set[str]:
    """Множество официальных name_en (для проверки 'официальности' депа в рантайме)."""
    return {e["name_en"].strip() for e in catalog if e.get("name_en")}
