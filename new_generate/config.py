"""Настроечные константы для new_generate — сгруппированы по тому, какую
часть раскладки/сборки данных они настраивают, а не свалены в один плоский
список: имя группы сразу говорит, к чему относится значение внутри.

Простые датаклассы без переопределения через переменные окружения — никто
не крутит число итераций ForceAtlas2 через env, проще поправить число тут.

Константы, нужные ровно одной функции без вариации между вызовами
(например, координатное пространство фронтенда, сигма разброса при
подмешивании) — локальные, рядом с этой функцией в layout.py, а не здесь.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeThresholds:
    """Минимальные веса, ниже которых ребро вообще не идёт в экспорт."""

    coauth_min_w: int = 2
    """Мин. число совместных публикаций для ребра автор-автор."""
    pub_edge_min_w: int = 3
    """Мин. число общих ИТМО-авторов для ребра публикация-публикация."""
    pub_layout_top_k: int = 6
    """Сколько сильнейших "общий автор"-рёбер оставлять на публикацию для раскладки (не для экспорта)."""


EDGE_THRESHOLDS = EdgeThresholds()


@dataclass(frozen=True)
class Fa2Iterations:
    """Число итераций ForceAtlas2, по одному прогону на тип сущности."""

    authors: int = 300
    pubs: int = 250
    repos: int = 100


FA2_ITERATIONS = Fa2Iterations()


@dataclass(frozen=True)
class SyntheticDeptEdges:
    """Синтетические слабые рёбра "тот же департамент" — не настоящие связи,
    а подсказка для раскладки, чтобы департамент не расползался бесформенным
    облаком (см. `layout.py::sparse_dept_edges`)."""

    dept_edge_k: int = 3
    """Со сколькими случайными коллегами по департаменту связан каждый узел (авторы)."""
    dept_edge_weight: float = 1.0
    """Сопоставимо с реальными рёбрами (совместные публикации начинаются с 1.0)."""
    pub_dept_edge_k: int = 1
    pub_dept_edge_weight: float = 0.5


SYNTHETIC_DEPT_EDGES = SyntheticDeptEdges()


@dataclass(frozen=True)
class MinSeparation:
    """Минимальное расстояние между узлами — проход после самой раскладки
    (`layout.py::spread_min_distance`), чтобы совпавшие точки не слипались
    в закрашенную кляксу."""

    authors: float = 4.5
    pubs: float = 3.5


MIN_SEPARATION = MinSeparation()


# --- Заглушки для отображения ------------------------------------------------------
NO_DEPT_NAME = "Без департамента"
NO_DEPT_NAME_EN = "No department"
NO_DEPT_COLOR = "#8a8f98"
