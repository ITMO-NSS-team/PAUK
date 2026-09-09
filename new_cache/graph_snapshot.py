"""Жизненный цикл файла-снепшота графа: запись, чтение, проверка свежести.

`write_snapshot`/`read_snapshot` не знают, что именно лежит внутри `graph`
(это забота `export.py::load_db()`) — их работа только в том, чтобы файл на
диске нельзя было случайно скормить коду, который ждёт другую версию формата.

`is_fresh` жил раньше отдельным файлом (`freshness.py`) — вынесен обратно
сюда: это тоже часть жизненного цикла того же самого файла на диске (когда
его в последний раз записывали), а не отдельная сущность, которой нужен
собственный модуль ради одной функции на десяток строк.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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


def is_fresh(path: Path, max_age: timedelta) -> bool:
    """Проверяет, что файл снепшота существует и не старше заданного TTL.

    Снепшот — не то, что пересобирается на каждый запуск: это дорогой шаг
    (тринадцать запросов к боевому Neo4j), поэтому его снимают вручную
    командой `pauk cache export` и переиспользуют. `is_fresh` даёт способ
    спросить "а не пора ли пересобрать", сравнивая возраст файла с
    допустимым TTL по mtime — не открывая и не парся сам файл целиком ради
    простого вопроса "не протухло ли".

    Аргументы:
        path: Путь к файлу снепшота на диске.
        max_age: Максимально допустимый возраст файла, после которого он
            считается устаревшим.

    Возвращает:
        `False`, если файла нет вообще (отсутствующий снепшот — это тоже
        "не свежий", а не особый случай, который нужно обрабатывать отдельно
        в вызывающем коде). Иначе — `True`, если время с последней записи
        файла не превышает `max_age`.

    Пример:
        >>> from pathlib import Path
        >>> from datetime import timedelta
        >>> is_fresh(Path("/несуществующий/файл.json"), timedelta(days=1))
        False
    """
    if not path.is_file():
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return datetime.now(UTC) - modified <= max_age
