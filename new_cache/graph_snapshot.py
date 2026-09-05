"""Конверт вокруг словаря графа: версия схемы + дата снятия + сами данные.

`write_snapshot`/`read_snapshot` не знают, что именно лежит внутри `graph`
(это забота `export.py::load_db()`) — их работа только в том, чтобы файл на
диске нельзя было случайно скормить коду, который ждёт другую версию формата.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pauk.storage import AtomicWriter

SCHEMA_VERSION = 1
"""Версия формата снепшота. Поднимать при любом несовместимом изменении формы
`graph` (переименование ключа, смена типа поля и т.п.) — старые снепшоты с
предыдущей версией `read_snapshot` тогда осознанно отклонит, вместо того
чтобы молча скормить их коду, который ждёт новую форму данных."""


def write_snapshot(path: Path, graph: dict[str, list]) -> None:
    """Атомарно записывает снепшот графа на диск.

    Оборачивает сырой словарь `graph` в конверт с версией схемы и временем
    снятия, затем пишет через `AtomicWriter` — так частично записанный файл
    (например, при обрыве процесса на середине `json.dump`) никогда не
    подменит собой предыдущий рабочий снепшот: запись идёт во временный файл
    рядом, а замена исходного — одной атомарной операцией `os.replace`.

    Аргументы:
        path: Путь, по которому нужно сохранить снепшот.
        graph: Плоский словарь таблиц графа — ровно то, что возвращает
            `export.py::load_db()`.

    Пример:
        >>> write_snapshot(Path("/tmp/snapshot.json"), {"persons": []})
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "graph": graph,
    }
    with AtomicWriter(path) as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))


def read_snapshot(path: Path) -> dict[str, list]:
    """Читает и проверяет снепшот графа, записанный `write_snapshot`.

    Аргументы:
        path: Путь к файлу снепшота.

    Возвращает:
        Словарь `graph` — те же таблицы, что были переданы в `write_snapshot`.

    Исключения:
        ValueError: версия схемы файла не совпадает с `SCHEMA_VERSION`
            (снепшот от несовместимой версии кода) либо в файле вообще нет
            ключа `graph` с объектом внутри.

    Пример:
        >>> read_snapshot(Path("/tmp/snapshot.json"))
        {'persons': []}
    """
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported graph snapshot schema: {payload.get('schema_version')}")
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("graph snapshot has no graph object")
    return graph
