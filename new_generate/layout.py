"""Чистая математика раскладки: позиционирование FA2, подгонка координат,
раздвижение коллизий. Не знает про авторов/публикации/репозитории как
понятия предметной области — только id узлов (везде `str`) и веса рёбер.
Как это вызывается для каждого типа сущности — см. `generate_data.py`.
"""

from __future__ import annotations

import colorsys
import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree  # type: ignore

from .config import DEPT_EDGE_K, DEPT_EDGE_WEIGHT


def golden_color(i: int) -> str:
    """Цвет департамента: шаг по золотому сечению для оттенка, HLS l=0.6 s=0.4.

    Аргументы:
        i: Порядковый номер департамента (после реиндексации по размеру).

    Возвращает:
        Цвет в формате `#rrggbb`.

    Пример:
        >>> golden_color(0)
        '#c27070'
    """
    hue = (i * 0.618033988749895) % 1.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.6, 0.4)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def dense_rank(values: dict[str, int]) -> dict[str, float]:
    """rank = плотный ранг метрики / число уникальных значений, округлено до 3.

    Аргументы:
        values: Метрика по id узла (например, число публикаций автора).

    Возвращает:
        Тот же набор ключей, значение — доля от 0 до 1 (чем больше метрика,
        тем ближе к 1).

    Пример:
        >>> dense_rank({"a": 1, "b": 5, "c": 5})
        {'a': 0.5, 'b': 1.0, 'c': 1.0}
    """
    uniq = sorted(set(values.values()))
    pos = {v: (i + 1) / len(uniq) for i, v in enumerate(uniq)}
    return {k: round(pos[v], 3) for k, v in values.items()}


# Координатное пространство фронтенда: 0..1000 (core.js: S = 1000)
COORD_MIN, COORD_MAX = 30.0, 970.0


def fit_coords(pos: Mapping[str, Sequence[float]]) -> dict[str, tuple[float, float]]:
    """Вписывает координаты FA2 в [COORD_MIN, COORD_MAX], сохраняя пропорции.

    Аргументы:
        pos: Позиции узлов из `networkx.forceatlas2_layout` (numpy-массивы
            `[x, y]`) или уже готовые кортежи `(x, y)` — сюда годится и то,
            и другое, функция сразу приводит оба конца пары к `float`.

    Возвращает:
        Те же id, координаты пересчитаны и округлены до 0.1.
    """
    if not pos:
        return {}
    pos = {k: (float(p[0]), float(p[1])) for k, p in pos.items()}
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    scale = (COORD_MAX - COORD_MIN) / span
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    return {k: (round(500.0 + (x - cx) * scale, 1), round(500.0 + (y - cy) * scale, 1)) for k, (x, y) in pos.items()}


def spread_min_distance(
    pos: dict[str, tuple[float, float]], d_min: float, seed: int, iters: int = 800
) -> dict[str, tuple[float, float]]:
    """Раздвигает любую пару узлов ближе d_min друг к другу — сошедшиеся
    кластеры FA2 иначе достаточно плотные, чтобы отрисоваться сплошной
    закрашенной кляксой вместо облака точек.

    Аргументы:
        pos: Позиции узлов после раскладки.
        d_min: Минимально допустимое расстояние между двумя узлами.
        seed: Сид генератора случайных чисел (для воспроизводимости).
        iters: Максимум итераций раздвижения.

    Возвращает:
        Те же id, координаты раздвинуты и округлены до 0.1.
    """
    keys = list(pos)
    P = np.array([pos[k] for k in keys], dtype=float)
    rng = np.random.RandomState(seed)
    for _ in range(iters):
        pairs = cKDTree(P).query_pairs(d_min, output_type="ndarray")
        # горстка отставших (узлы, прижатые к границе карты) — это нормально
        if len(pairs) <= max(2, len(keys) // 2000):
            break
        delta = P[pairs[:, 0]] - P[pairs[:, 1]]
        dist = np.hypot(delta[:, 0], delta[:, 1])
        coincident = dist < 1e-9
        if coincident.any():
            delta[coincident] = rng.uniform(-1, 1, (int(coincident.sum()), 2))
            dist[coincident] = np.hypot(delta[coincident, 0], delta[coincident, 1])
        dirv = delta / dist[:, None]
        push = ((d_min - dist) * 0.45)[:, None] * dirv
        np.add.at(P, pairs[:, 0], push)
        np.subtract.at(P, pairs[:, 1], push)
        np.clip(P, 30.0, 970.0, out=P)
    return {k: (round(float(x), 1), round(float(y), 1)) for k, (x, y) in zip(keys, P, strict=True)}


def sparse_dept_edges(
    all_ids: Iterable[str],
    dept_of: dict[str, str | None],
    rng: random.Random,
    k: int = DEPT_EDGE_K,
    weight: float = DEPT_EDGE_WEIGHT,
    taper_size: int | None = None,
) -> dict[tuple[str, str], float]:
    """Слабые рёбра "тот же департамент": каждый узел связывается с k
    случайными коллегами по своему департаменту. Разреженный случайный граф
    -> органичное облако под FA2; хаб-узел на департамент вместо этого
    расставил бы свои листья идеальным кругом (кольца — ровно тот
    артефакт, который это заменяет).

    Аргументы:
        all_ids: Все id узлов (обычно множество — сортируется ниже, см. дальше).
        dept_of: Департамент узла по его id, `None`/отсутствие — без департамента.
        rng: Генератор случайных чисел (один на весь прогон раскладки).
        k: Сколько случайных коллег по департаменту берёт каждый узел.
        weight: Вес одного такого ребра.
        taper_size: Департаменты крупнее этого получают пропорционально более
            слабые рёбра, чтобы уже большие департаменты не схлопывались в
            бесформенный диск.

    Возвращает:
        Вес по каждой паре узлов `(a, b)` с `a < b`.
    """
    # all_ids обычно — множество; сортируем, чтобы группировка по департаментам
    # (и то, сколько общего состояния rng съедает каждый департамент) была
    # одинаковой при каждом прогоне с одним seed, а не перемешивалась
    # рандомизацией хеша строк между процессами.
    by_dept: dict[str, list[str]] = defaultdict(list)
    for i in sorted(all_ids):
        d = dept_of.get(i)
        if d:
            by_dept[d].append(i)
    edges: dict[tuple[str, str], float] = {}
    for members in by_dept.values():
        if len(members) < 2:
            continue
        w = weight
        if taper_size and len(members) > taper_size:
            w = weight * taper_size / len(members)
        members = sorted(members)
        for i in members:
            others = [m for m in members if m != i]
            for j in rng.sample(others, min(k, len(others))):
                edges[(i, j) if i < j else (j, i)] = w
    return edges


# Сигма разброса при подмешивании (в финальных 0..1000 единицах) и
# минимальный интервал между отставшими узлами (размер ячейки сетки)
# для прохода подмешивания в fa2_blended_layout.
STRANDED_JITTER = 55.0
STRANDED_MIN_SEP = 7.0


def fa2_blended_layout(
    edge_weights: dict[tuple[str, str], float], all_ids: Iterable[str], max_iter: int, seed: int
) -> tuple[dict[str, tuple[float, float]], tuple[int, int, int, int]]:
    """FA2 только над ГИГАНТСКОЙ связной компонентой, всё остальное
    подмешивается после. Это не оптимизация, а необходимость: несвязанные
    компоненты только отталкиваются друг от друга и расходятся без предела
    с ростом итераций, поэтому реальное содержимое схлопывается в точку при
    масштабировании ("всё свалено в центр"), если FA2 запустить на полном графе.

    Маленькие компоненты (>=2 узлов) садятся вместе одним плотным пятном,
    чтобы соавторы оставались рядом; настоящие синглтоны разбрасываются по
    отдельности с джиттером на грубой сетке занятости, чтобы не собираться
    комком или кольцом.

    Аргументы:
        edge_weights: Вес по каждой паре узлов `(a, b)`.
        all_ids: Все id узлов, включая те, что не участвуют ни в одном ребре.
        max_iter: Число итераций ForceAtlas2 для гигантской компоненты.
        seed: Сид (используется и для FA2, и для подмешивания — второе
            берёт `seed + 1`, чтобы не повторять в точности случайности FA2).

    Возвращает:
        Кортеж `(pos, stats)`, где `pos` — позиции всех узлов из `all_ids`,
        а `stats` — `(число узлов гиганта, число рёбер гиганта, число
        маленьких компонент, число синглтонов)` для логирования.
    """
    # all_ids обычно — множество; сортируем, чтобы порядок добавления узлов
    # (от которого зависят начальные позиции FA2 при заданном seed, через
    # enumerate(G)) был одинаковым при каждом прогоне с одним seed, а не
    # перемешивался рандомизацией хеша строк между процессами.
    G = nx.Graph()
    G.add_nodes_from(sorted(all_ids))
    G.add_weighted_edges_from((a, b, w) for (a, b), w in edge_weights.items())
    comps = list(nx.connected_components(G))
    giant = max(comps, key=len) if comps else set()
    if len(giant) < 2:
        giant = set()
    small = sorted((c for c in comps if c is not giant and len(c) >= 2), key=len, reverse=True)
    singles = sorted(n for c in comps if c is not giant and len(c) == 1 for n in c)

    pos: dict[str, tuple[float, float]] = {}
    if giant:
        # G.subgraph() строит свой вид на основе множества внутри, поэтому
        # порядок узлов там всё ещё зависит от рандомизации хеша, даже если
        # giant заранее отсортирован — копируем в свежий граф с явным
        # порядком вместо этого.
        sub = nx.Graph()
        sub.add_nodes_from(sorted(giant))
        sub.add_weighted_edges_from((a, b, d["weight"]) for a, b, d in G.edges(data=True) if a in giant and b in giant)
        pos = fit_coords(nx.forceatlas2_layout(sub, max_iter=max_iter, weight="weight", seed=seed))  # type: ignore[arg-type]

    rng = random.Random(seed + 1)
    crowd = list(pos.values()) or [(500.0, 500.0)]
    occupied: set[tuple[int, int]] = set()
    cell = STRANDED_MIN_SEP

    def place(x: float, y: float) -> tuple[float, float]:
        x = min(970.0, max(30.0, x))
        y = min(970.0, max(30.0, y))
        occupied.add((int(x // cell), int(y // cell)))
        return round(x, 1), round(y, 1)

    def free_spot(gen: Callable[[], tuple[float, float]]) -> tuple[float, float]:
        # range(60) никогда не пуст, x/y всегда будут переприсвоены — но
        # статический анализ этого не знает, поэтому нужна начальная
        # заглушка, которая ни разу не попадёт в place() по-настоящему.
        x, y = 0.0, 0.0
        for _attempt in range(60):
            x, y = gen()
            x = min(970.0, max(30.0, x))
            y = min(970.0, max(30.0, y))
            if (int(x // cell), int(y // cell)) not in occupied:
                break
        return place(x, y)

    for comp in small:
        ax, ay = rng.choice(crowd)
        ccx = min(940.0, max(60.0, rng.gauss(ax, STRANDED_JITTER)))
        ccy = min(940.0, max(60.0, rng.gauss(ay, STRANDED_JITTER)))
        radius = 6.0 + 2.2 * math.sqrt(len(comp))
        sx, sy = rng.uniform(0.55, 1.6), rng.uniform(0.55, 1.6)  # растяжение/поворот, чтобы пятна не были ровными кругами
        ang = rng.uniform(0.0, math.pi)
        cos_a, sin_a = math.cos(ang), math.sin(ang)

        def patch_point(
            radius: float = radius,
            sx: float = sx,
            sy: float = sy,
            ccx: float = ccx,
            ccy: float = ccy,
            cos_a: float = cos_a,
            sin_a: float = sin_a,
        ) -> tuple[float, float]:
            dx, dy = rng.gauss(0, radius * sx), rng.gauss(0, radius * sy)
            return ccx + dx * cos_a - dy * sin_a, ccy + dx * sin_a + dy * cos_a

        for n in sorted(comp):
            pos[n] = free_spot(patch_point)

    for n in singles:

        def gen(ax: float = 0, ay: float = 0) -> tuple[float, float]:
            ax, ay = rng.choice(crowd)
            return rng.gauss(ax, STRANDED_JITTER), rng.gauss(ay, STRANDED_JITTER)

        pos[n] = free_spot(gen)

    stats = (len(giant), G.subgraph(giant).number_of_edges(), len(small), len(singles))
    return pos, stats


def majority_dept(dept_lists: Iterable[Iterable[str]]) -> str | None:
    """Департамент по большинству голосов; ничьи разрешаются по id (не по
    глобальной популярности — это создало бы петлю обратной связи
    "богатый богатеет" в пользу и без того крупных департаментов).

    Аргументы:
        dept_lists: Список департаментов на каждого "избирателя" (например,
            список департаментов каждого соавтора публикации).

    Возвращает:
        Id департамента-победителя, или `None`, если голосов не было вовсе.

    Пример:
        >>> majority_dept([["d1"], ["d1", "d2"], ["d2"]])
        'd1'
    """
    cnt: Counter[str] = Counter()
    for depts in dept_lists:
        for d in depts:
            cnt[d] += 1
    if not cnt:
        return None
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
