"""Проверка свежести файла-снепшота графа.

Снепшот (`data/cache/graph_snapshot.json`) — не то, что пересобирается на
каждый запуск: это дорогой шаг (десять запросов к боевому Neo4j), поэтому
его снимают вручную командой `pauk cache export` и переиспользуют. `is_fresh`
даёт способ спросить "а не пора ли пересобрать", сравнивая возраст файла с
допустимым TTL, не читая сам файл целиком.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path


def is_fresh(path: Path, max_age: timedelta) -> bool:
    """Проверяет, что файл существует и не старше заданного TTL.

    Смотрит только на mtime файла — открывать и парсить сам снепшот, чтобы
    проверить его "свежесть", было бы лишней работой ради простого вопроса
    "не протухло ли".

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
