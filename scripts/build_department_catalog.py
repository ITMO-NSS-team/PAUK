"""Одноразовый генератор draft официального каталога департаментов ИТМО.

Скрейпит ПОЛНОЕ дерево научной структуры с RU-страницы
(`ITMO_STRUCTURE_URL_RU`, «Основные образовательные и научные подразделения»):
факультеты → институты → центры → лаборатории. Иерархию восстанавливает по глубине
вложенности `<ul>` (линейный скан по сбалансированным тегам — надёжно к незакрытым
`<li>`, на которых DOM-парсеры путают предков). Не-научные единицы (админ/производство/
профориентация) отфильтровываются. Английские имена берутся с EN-страницы структуры
(`ITMO_STRUCTURE_URL_EN`) по общему числовому id — официально для faculty/центр-тира.

Юниты без официального EN (листовые лаборатории — у них нет ни id, ни EN-страницы)
остаются с ПУСТЫМ name_en: их заполняют отдельно (LLM RU→EN + ручная сверка), а
авторские написания добавляются в name_variants из корпуса аффилиаций. Всё это —
одноразовые доводочные шаги; см. историю issue #40.

НЕ входит в пайплайн. Результат — DRAFT: ОБЯЗАТЕЛЬНО сверяется вручную и дополняется
перед коммитом как data/departments_catalog.json (источник истины для seed_departments).

Запуск из корня проекта:
    uv run python scripts/build_department_catalog.py
    uv run python scripts/build_department_catalog.py --out data/departments_catalog.draft.json
"""

import argparse
import json
import logging
import re
from pathlib import Path

import requests

from config import (
    DEPARTMENTS_CATALOG_PATH,
    DOWNLOAD_TIMEOUT,
    ITMO_STRUCTURE_URL_EN,
    ITMO_STRUCTURE_URL_RU,
    USER_AGENT,
)

logger = logging.getLogger(__name__)

# По умолчанию пишем в DRAFT, чтобы наивный ре-ран не затёр выверенный источник истины
# (departments_catalog.json). Промоут draft → каталог — вручную после сверки.
_DRAFT_PATH = DEPARTMENTS_CATALOG_PATH.with_name("departments_catalog.draft.json")

# Границы научной секции на RU-странице.
_SECTION_START = "Основные образовательные и научные"
_SECTION_END = "Административные (сервисные)"
# Ссылка-подразделение: /ru|en/[view]faculty|unit|department|otherstructure/<id>/...>ИМЯ<
_LINK_RE = re.compile(
    r"/(?:ru|en)/(?:view)?(?:faculty|unit|department|otherstructure)/(\d+)/[^\"'>]*\"[^>]*>\s*([^<]+?)\s*<",
    re.I,
)
# Токены дерева для линейного скана глубины.
_TOKEN_RE = re.compile(r"(<ul\b)|(</ul>)|(<a\b[^>]*>(.*?)</a>)", re.I | re.S)
# Не-научные единицы внутри научного дерева — исключаем.
_DROP_RE = re.compile(
    r"^отдел\b|опытно-экспериментальное производство|учебно-методическое объединение|"
    r"^ресурсный центр$|военный учебный центр|базовая профориентационная школа|"
    r"дополнительного образования для школьников|центр развития карьеры|"
    r"учебно-практический центр",
    re.I,
)


def fetch(url: str) -> str:
    """GET страницы с полит-агентом; кидает исключение при ошибке."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _clean(raw: str) -> str:
    """Схлопывает пробелы, снимает html-теги/сущности из фрагмента."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw or "")).strip()


def parse_ru_tree(html: str) -> list[dict]:
    """Юниты научной секции с иерархией по глубине <ul>.

    Возвращает список {name_ru, faculty_id, school_ru} в порядке документа; school_ru —
    ближайший top-level предок (мегафакультет/самостоятельный топ-юнит), у самих
    top-level — они сами. Не-научные единицы отфильтрованы.
    """
    start = html.find(_SECTION_START)
    end = html.find(_SECTION_END)
    if start == -1 or end == -1:
        raise SystemExit("Не найдены границы научной секции на RU-странице структуры.")
    segment = html[start:end]

    id_by_name = {name: int(fid) for fid, name in _LINK_RE.findall(segment)}  # href-имена → id
    depth, last_top, rows = 0, "", []
    for token in _TOKEN_RE.finditer(segment):
        if token.group(1):
            depth += 1
        elif token.group(2):
            depth = max(0, depth - 1)
        elif token.group(3):
            name = _clean(token.group(4))
            if not name or _DROP_RE.search(name):
                continue
            if depth == 1:
                last_top = name
            rows.append({"name_ru": name, "faculty_id": id_by_name.get(name),
                         "school_ru": name if depth == 1 else last_top})
    # дедуп по (name_ru, school_ru), порядок появления
    seen, uniq = set(), []
    for row in rows:
        key = (row["name_ru"].lower(), row["school_ru"].lower())
        if key not in seen:
            seen.add(key)
            uniq.append(row)
    return uniq


def official_en_by_id(html: str) -> dict[int, str]:
    """id → официальное англ. имя с EN-страницы структуры (faculty/центр-тир)."""
    return {int(fid): _clean(name) for fid, name in _LINK_RE.findall(html)}


def build_catalog() -> list[dict]:
    """Собирает draft: RU-дерево (иерархия) + официальный EN по id; EN лабов пуст."""
    ru_units = parse_ru_tree(fetch(ITMO_STRUCTURE_URL_RU))
    en_by_id = official_en_by_id(fetch(ITMO_STRUCTURE_URL_EN))
    school_en = {u["name_ru"]: en_by_id.get(u["faculty_id"], "")
                 for u in ru_units if u["school_ru"] == u["name_ru"]}
    logger.info("Научных юнитов: %d | официальных EN с EN-страницы: %d",
                len(ru_units), len(en_by_id))

    catalog, no_en = [], 0
    for u in ru_units:
        name_en = en_by_id.get(u["faculty_id"], "")
        if not name_en:
            no_en += 1
        catalog.append({
            "faculty_id": u["faculty_id"],
            "school_en": school_en.get(u["school_ru"], ""),
            "school_ru": u["school_ru"],
            "name_en": name_en,
            "name_ru": u["name_ru"],
            "aliases": [],
        })
    catalog.sort(key=lambda d: (d["school_ru"].lower(), d["name_ru"].lower()))
    logger.warning("Без официального EN (заполнить LLM RU→EN + вручную): %d из %d",
                   no_en, len(catalog))
    return catalog


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Сгенерировать draft официального каталога департаментов ИТМО."
    )
    parser.add_argument("--out", type=Path, default=_DRAFT_PATH,
                        help="Куда писать draft JSON (по умолчанию — *.draft.json рядом с "
                             "каталогом; боевой departments_catalog.json не перезаписывается).")
    args = parser.parse_args()

    catalog = build_catalog()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Записано %d записей в %s. СВЕРЬТЕ ВРУЧНУЮ, заполните EN лабов (LLM RU→EN) "
                "и name_variants из корпуса, затем промоут → %s.",
                len(catalog), args.out, DEPARTMENTS_CATALOG_PATH.name)


if __name__ == "__main__":
    main()
