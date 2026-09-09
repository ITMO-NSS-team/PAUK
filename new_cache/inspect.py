"""Read-only просмотр снепшота графа (`graph_snapshot.py`) без ручного разбора
сырого JSON: `write_snapshot()` пишет файл однострочником, без `indent`, ради
компактности (снепшот легко занимает десятки МБ) — открывать его текстовым
редактором не рассчитано. Поля из `pauk/graph/extract.py::JSON_TEXT_FIELDS`
(`funding`/`versions`/`counts_by_year`/`affiliations`) при этом хранятся как
уже сериализованная JSON-строка (Neo4j не умеет вложенные map/list-of-map) —
в сыром файле это лес экранированных кавычек, здесь они раскрываются обратно
в объект перед печатью.

CLI: `python -m new_cache.inspect data/cache/graph_snapshot_v2.json` (список
таблиц), `... --table persons` (доля null/типы по каждому полю таблицы),
`... --table persons --sample 3` (N первых строк, читаемо).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pauk.graph.extract import JSON_TEXT_FIELDS

from .graph_snapshot import read_snapshot


def _decode_json_text(field: str, value: Any) -> Any:
    """Разворачивает JSON-текстовое поле обратно в объект для читаемой печати.

    Аргументы:
        field: Имя поля — решение "разворачивать или нет" принимается только
            по имени, не по содержимому (см. `JSON_TEXT_FIELDS`).
        value: Значение поля из снепшота.

    Возвращает:
        Разобранный объект, если `field` в `JSON_TEXT_FIELDS` и `value` —
        валидная JSON-строка. Иначе — `value` без изменений (в частности,
        `None` и битый JSON остаются как есть, не падают).
    """
    if field not in JSON_TEXT_FIELDS or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def summarize(graph: dict[str, list]) -> None:
    """Печатает список таблиц снепшота: сколько строк и какие поля у каждой."""
    for name, rows in graph.items():
        fields = ", ".join(rows[0].keys()) if rows else "(нет строк)"
        print(f"{name}: {len(rows)} строк")
        print(f"  поля: {fields}")


def describe_table(rows: list[dict], table: str) -> None:
    """Печатает по каждому полю таблицы: сколько `null`, какие типы у
    непустых значений, JSON-текстовое ли поле — то, что нужно, чтобы понять
    "это реальная разреженность данных или дыра в пайплайне" не открывая
    файл руками.
    """
    if not rows:
        print(f"{table}: нет строк")
        return

    for field in rows[0]:
        values = [row.get(field) for row in rows]
        non_null = [v for v in values if v is not None]
        n_null = len(values) - len(non_null)
        pct_null = 100 * n_null / len(values)
        types = sorted({type(v).__name__ for v in non_null})
        marker = "  [JSON-текст]" if field in JSON_TEXT_FIELDS else ""
        print(f"  {field:20s} null={n_null:6d} ({pct_null:5.1f}%)  типы={types}{marker}")


def sample_rows(rows: list[dict], n: int) -> None:
    """Печатает `n` первых строк таблицы, с раскрытыми JSON-текстовыми полями
    (см. `_decode_json_text`) вместо сырых экранированных строк.
    """
    for row in rows[:n]:
        decoded = {field: _decode_json_text(field, value) for field, value in row.items()}
        print(json.dumps(decoded, ensure_ascii=False, indent=2))
        print("---")


def main() -> None:
    parser = argparse.ArgumentParser(description="Просмотр снепшота графа (new_cache) без ручного разбора сырого JSON")
    parser.add_argument("snapshot", type=Path, help="путь к файлу снепшота, например data/cache/graph_snapshot_v2.json")
    parser.add_argument("--table", help="имя таблицы (persons/publications/...) — без флага печатается только список таблиц")
    parser.add_argument("--sample", type=int, default=0, help="напечатать N первых строк таблицы вместо статистики по полям (нужен --table)")
    args = parser.parse_args()

    graph = read_snapshot(args.snapshot)

    if args.table is None:
        summarize(graph)
        return

    if args.table not in graph:
        parser.error(f"нет такой таблицы: {args.table!r}. Доступные: {', '.join(graph)}")

    rows = graph[args.table]
    if args.sample:
        sample_rows(rows, args.sample)
    else:
        describe_table(rows, args.table)


if __name__ == "__main__":
    main()
