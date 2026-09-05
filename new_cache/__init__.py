"""new_cache — переписанный слой снятия снепшота графа из Neo4j.

Рабочая директория для переписки `pauk/cache/` "набело": тот же язык (Python)
и те же зависимости (`neo4j`, `pauk.settings`, `pauk.storage.AtomicWriter`),
но с исправленным запросом персон (свойство `is_itmo` вместо снятой метки
`:Itmo`/`:External`), полными докстрингами на русском и тестовым покрытием.
По завершении переписки этот пакет должен физически занять место
`pauk/cache/`, а не остаться вечным дублем рядом с ним.
"""

from .export import GraphSnapshotExporter
from .freshness import is_fresh
from .graph_snapshot import read_snapshot, write_snapshot

__all__ = ["GraphSnapshotExporter", "is_fresh", "read_snapshot", "write_snapshot"]
