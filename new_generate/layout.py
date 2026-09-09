"""Раскладка графа: FA2-позиционирование, подгонка координат, раздвижение
коллизий — плюс сама сборка трёх раскладок (авторы/публикации/репозитории)
из снепшота. Не знает про личные поля авторов/публикаций/репозиториев —
только id узлов (везде `str`) и веса рёбер. Как результат используется для
сборки узлов/рёбер — см. `nodes.py`/`edges.py`.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree  # type: ignore

from .authorship import Authorship
from .config import EDGE_THRESHOLDS, FA2_ITERATIONS, MIN_SEPARATION, SYNTHETIC_DEPT_EDGES
from .departments import DepartmentAssignment

logger = logging.getLogger(__name__)

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
        np.clip(P, COORD_MIN, COORD_MAX, out=P)
    return {k: (round(float(x), 1), round(float(y), 1)) for k, (x, y) in zip(keys, P, strict=True)}


def sparse_dept_edges(
    all_ids: Iterable[str],
    dept_of: dict[str, str | None],
    rng: random.Random,
    k: int = SYNTHETIC_DEPT_EDGES.dept_edge_k,
    weight: float = SYNTHETIC_DEPT_EDGES.dept_edge_weight,
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
        x = min(COORD_MAX, max(COORD_MIN, x))
        y = min(COORD_MAX, max(COORD_MIN, y))
        occupied.add((int(x // cell), int(y // cell)))
        return round(x, 1), round(y, 1)

    def free_spot(gen: Callable[[], tuple[float, float]]) -> tuple[float, float]:
        # range(60) никогда не пуст, x/y всегда будут переприсвоены — но
        # статический анализ этого не знает, поэтому нужна начальная
        # заглушка, которая ни разу не попадёт в place() по-настоящему.
        x, y = 0.0, 0.0
        for _attempt in range(60):
            x, y = gen()
            x = min(COORD_MAX, max(COORD_MIN, x))
            y = min(COORD_MAX, max(COORD_MIN, y))
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


class ForceAtlasLayouter:
    """Раскладка ForceAtlas2 — держит `seed` как состояние вместо параметра
    в каждом отдельном вызове (иначе он протаскивается через всю цепочку
    вызовов в `GraphLayoutBuilder` без изменений). Два метода — ровно два
    паттерна использования, которые реально есть в этом проекте:
    `blended()` для авторов/публикаций (смешивание маленьких компонент +
    раздвижение коллизий), `simple()` для репозиториев (голый FA2, без
    того и другого — граф репозиториев обычно достаточно разрежен, чтобы
    в этом не нуждаться).
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def blended(
        self, edge_weights: dict[tuple[str, str], float], all_ids: Iterable[str], max_iter: int, min_sep: float
    ) -> tuple[dict[str, tuple[float, float]], tuple[int, int, int, int]]:
        """FA2 с подмешиванием несвязанных компонент + раздвижение коллизий."""
        pos, stats = fa2_blended_layout(edge_weights, all_ids, max_iter, self.seed)
        return spread_min_distance(pos, min_sep, self.seed), stats

    def simple(self, graph: nx.Graph, max_iter: int) -> dict[str, tuple[float, float]]:
        """Голый FA2 без подмешивания/раздвижения — граф уже связный или
        разрежен настолько, что это не нужно."""
        return fit_coords(nx.forceatlas2_layout(graph, max_iter=max_iter, weight="weight", seed=self.seed))  # type: ignore[arg-type]


@dataclass(frozen=True)
class Layout:
    """Позиции узлов после раскладки + веса рёбер, которые реально идут на
    экспорт (не путать с весами, которые использовались только чтобы
    ПОСЧИТАТЬ раскладку — те шире: `coauth`/`pub_pair_w` здесь ýже, чем то,
    что видит FA2, потому что раскладка дополнительно учитывает общие
    репозитории и синтетические "тот же департамент"-рёбра, а на карточку
    "общие публикации"/"общие авторы" в интерфейсе должны попадать только
    настоящие связи)."""

    pos_authors: dict[str, tuple[float, float]]
    """Ключ автора -> координаты (x, y) в пространстве фронтенда."""
    pos_pubs: dict[str, tuple[float, float]]
    """Ключ публикации -> координаты (x, y)."""
    pos_repos: dict[str, tuple[float, float]]
    """Ключ репозитория -> координаты (x, y)."""
    coauth: dict[tuple[str, str], int]
    """Пары авторов -> число совместных публикаций (настоящее, для coauth_edges)."""
    pub_pair_w: dict[tuple[str, str], int]
    """Пары публикаций -> число общих ИТМО-авторов (настоящее, для pub_edges)."""
    repo_edge_w: dict[tuple[str, str], int]
    """Пары репозиториев -> число общих публикаций (используется и для раскладки, и для repo_edges — здесь раздвоения нет)."""


class GraphLayoutBuilder:
    """Считает три раскладки ForceAtlas2 (авторы/публикации/репозитории) —
    у каждой своя мера близости, см. `docs/architecture/gui.md`. Держит
    `db`/`authorship`/`assignment` как состояние, чтобы `build()` не тащил
    их параметрами — они одни и те же на все три раскладки внутри одного вызова.
    """

    def __init__(self, db: dict[str, list[dict]], authorship: Authorship, assignment: DepartmentAssignment) -> None:
        self.db = db
        self.authorship = authorship
        self.assignment = assignment

    def build(self, seed: int) -> Layout:
        """Считает три раскладки ForceAtlas2 (авторы/публикации/репозитории) —
        у каждой своя мера близости, см. `docs/architecture/gui.md`.

        Аргументы:
            seed: Сид ForceAtlas2 и подмешивания несвязанных компонент.

        Возвращает:
            `Layout` с позициями и весами рёбер для экспорта.
        """
        rng = random.Random(seed)  # один общий генератор на sparse_dept_edges — авторов и публикаций, см. докстринг про seed+1
        layouter = ForceAtlasLayouter(seed)

        # --- авторы: совместные публикации + общие репозитории + разреженные
        # "тот же департамент"-рёбра. Первое (coauth) уходит и в раскладку, и
        # в экспорт как есть; репозитории и dept-рёбра — только в раскладку.
        # Каждая пара соавторов одной публикации — реальное ребро, вес =
        # число публикаций, написанных вместе.
        coauth: dict[tuple[str, str], int] = defaultdict(int)
        for _pid, pers in self.authorship.pub_authors.items():
            for a, b in combinations(sorted(set(pers)), 2):
                coauth[(a, b)] += 1

        # Обычный dict, а не Counter: дальше в него подмешиваются дробные веса
        # dept-рёбер (sparse_dept_edges), а Counter в typeshed типизирован
        # только под int. dict(coauth) копирует реальные веса как стартовые —
        # дальше только ДОБАВЛЯЕМ синтетику поверх, не заменяем.
        author_layout_w: dict[tuple[str, str], float] = dict(coauth)
        # Кто с кем участвовал в одном репозитории (CONTRIBUTED_TO) — тоже
        # повод сблизить узлы на карте, хотя в coauth_edges (экспорт) это
        # никогда не попадёт, только влияет на раскладку.
        repo_contributors: dict[str, set[str]] = defaultdict(set)
        for row in self.db["repo_persons"]:
            repo_contributors[row["rid"]].add(row["per"])
        for pers in repo_contributors.values():
            for a, b in combinations(sorted(pers), 2):
                author_layout_w[(a, b)] = author_layout_w.get((a, b), 0) + 1
        # Синтетические слабые рёбра "тот же департамент" — см. sparse_dept_edges,
        # только чтобы коллеги без единой реальной связи не разлетались по карте.
        for pair, w in sparse_dept_edges(set(self.assignment.static_depts), self.assignment.author_dept, rng).items():
            author_layout_w[pair] = author_layout_w.get(pair, 0) + w

        t0 = time.time()
        pos_authors, (n_giant, e_giant, n_small, n_single) = layouter.blended(
            author_layout_w, set(self.assignment.static_depts), FA2_ITERATIONS.authors, MIN_SEPARATION.authors
        )
        logger.info(
            "FA2 по авторам: гигант %d узлов / %d рёбер, подмешано: %d маленьких компонент + %d синглтонов, "
            "min-sep %.1f, %.1f с",
            n_giant, e_giant, n_small, n_single, MIN_SEPARATION.authors, time.time() - t0,
        )

        # --- публикации: общие ИТМО-авторы. Полный граф w>=1 — тысячи рёбер,
        # поэтому для раскладки берётся top-K сильнейших связей на публикацию;
        # на экспорт (pub_edges) идёт полный pub_pair_w, не урезанный.
        # Пара публикаций одного автора — ребро, вес = число общих авторов.
        pub_pair_w: dict[tuple[str, str], int] = defaultdict(int)
        for _per, plist in self.authorship.author_pubs.items():
            for a, b in combinations(sorted(set(plist)), 2):
                pub_pair_w[(a, b)] += 1

        t0 = time.time()
        # Для каждой публикации собираем список её соседей с весами связи —
        # с ОБЕИХ сторон ребра (a видит b, b видит a), чтобы можно было
        # честно отобрать top-K сильнейших для КАЖДОЙ публикации отдельно,
        # а не просто топ по всему графу разом.
        strongest: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for (a, b), w in pub_pair_w.items():
            strongest[a].append((w, b))
            strongest[b].append((w, a))
        pub_layout_w: dict[tuple[str, str], float] = {}
        for n, lst in strongest.items():
            lst.sort(key=lambda t: (-t[0], t[1]))  # сильнейшие сначала, ничьи — по id соседа для детерминизма
            for w, o in lst[: EDGE_THRESHOLDS.pub_layout_top_k]:
                pub_layout_w[(n, o) if n < o else (o, n)] = w  # ключ всегда (меньший, больший) — не дублировать ребро дважды
        # Та же синтетика "тот же департамент", что и у авторов выше, только
        # с более слабыми параметрами (см. PUB_DEPT_EDGE_K/WEIGHT) и taper_size —
        # у публикаций департаментов может быть намного больше сущностей на один.
        for pair, w in sparse_dept_edges(
            self.authorship.pub_ids,
            self.assignment.pub_primary,
            rng,
            k=SYNTHETIC_DEPT_EDGES.pub_dept_edge_k,
            weight=SYNTHETIC_DEPT_EDGES.pub_dept_edge_weight,
            taper_size=150,
        ).items():
            pub_layout_w[pair] = pub_layout_w.get(pair, 0) + w

        pos_pubs, (n_giant_p, e_giant_p, n_small_p, n_single_p) = layouter.blended(
            pub_layout_w, self.authorship.pub_ids, FA2_ITERATIONS.pubs, MIN_SEPARATION.pubs
        )
        logger.info(
            "FA2 по публикациям: гигант %d узлов / %d рёбер, подмешано: %d маленьких компонент + %d синглтонов, "
            "min-sep %.1f, %.1f с",
            n_giant_p, e_giant_p, n_small_p, n_single_p, MIN_SEPARATION.pubs, time.time() - t0,
        )

        # --- репозитории: общие публикации (включая публикации вне графа, у
        # которых нет ни одного ИТМО-автора — репозиторий их всё равно реализует,
        # поэтому здесь db["repo_pubs"] целиком, а не authorship.pub_ids).
        repo_all_pubs: dict[str, set[str]] = defaultdict(set)
        for row in self.db["repo_pubs"]:
            repo_all_pubs[row["rid"]].add(row["pid"])
        # Вес ребра — просто число публикаций, общих у пары репозиториев
        # (пересечение множеств); ноль общих публикаций — ребра вообще нет.
        repo_edge_w: dict[tuple[str, str], int] = {}
        for a, b in combinations(sorted(repo_all_pubs), 2):
            shared = len(repo_all_pubs[a] & repo_all_pubs[b])
            if shared:
                repo_edge_w[(a, b)] = shared

        # Простой граф без blended/spread (см. докстринг ForceAtlasLayouter.simple) —
        # репозиториев на порядок меньше, чем авторов/публикаций, граф разрежен.
        R = nx.Graph()
        R.add_nodes_from(r["id"] for r in self.db["repositories"])
        R.add_weighted_edges_from((a, b, w) for (a, b), w in repo_edge_w.items())
        pos_repos = layouter.simple(R, FA2_ITERATIONS.repos)

        # coauth/pub_pair_w/repo_edge_w — РЕАЛЬНЫЕ веса, идут и в раскладку
        # (через author_layout_w/pub_layout_w выше), и на экспорт как есть
        # (см. докстринг Layout про то, почему это разные вещи).
        return Layout(
            pos_authors=pos_authors,
            pos_pubs=pos_pubs,
            pos_repos=pos_repos,
            coauth=dict(coauth),
            pub_pair_w=dict(pub_pair_w),
            repo_edge_w=repo_edge_w,
        )
