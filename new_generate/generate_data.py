"""Снепшот `new_cache` -> раскладка -> JSON для сайта.

Переписка `pauk/gui/generate_data.py` под новую пару `new_cache`/`new_gui`:

- `db` теперь в форме `new_cache::load_db()` — списки словарей (`cypher_dict`
  для персон/публикаций/репозиториев), а не смесь словарей и позиционных
  кортежей, как в оригинале.
- `build_graph_data()` разбита на именованные приватные функции (раньше —
  340 строк в одной функции, без единого теста) — каждая тестируется
  отдельно, промежуточное состояние передаётся между ними через
  датаклассы (`Authorship`, `DepartmentAssignment`, `Layout`), а не через
  десяток отдельных словарей-аргументов.
- Узлы теперь строятся сразу в двух формах — "summary" (то, что нужно
  нарисовать точку на карте: `key`/`kind`/`dept`/`label`/`rank`/`gx`/`gy`
  плюс одна сводная цифра) и "detail" (всё остальное — расширенные поля,
  которые `new_gui` подгружает лениво после карты). Раньше так уже было
  сделано ровно для одной сущности (публикации — отдельный
  `build_search_detail()`/`graph-search.js`); здесь это обобщено на все
  четыре типа, а не изобретено заново.
- Департаменты пока БЕЗ отдельного detail-файла: сегодня в departments
  нет ни одного поля, которого не было бы уже в summary (это изменится,
  когда в `new_generate` попадёт иерархия `PART_OF` из `new_cache` —
  заводить пустой detail-файл заранее незачем).
- Вывод — голый JSON (не `window.GRAPH=...;`): `new_gui` уже умеет читать
  голый JSON (`core/data.ts::loadGraphData()`), обёртка была нужна только
  старому `pauk/gui/web/`, который сюда не относится.
- `--public`-сборка (для GitHub Pages) теперь не вырезает отдельные поля
  из объекта автора, а просто не пишет `authors-detail.json` вовсе — это
  надёжнее: забыть добавить новое личное поле в список на вырезание
  (риск оригинала) сейчас невозможно в принципе, потому что личные поля
  физически не существуют вне detail-файла.

`layout.py`/`config.py` не меняются логически (см. `new_generate/layout.py`,
сверено побитово с оригиналом на одинаковых входах).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import networkx as nx

from .config import (
    COAUTH_MIN_W,
    FA2_ITER_AUTHORS,
    FA2_ITER_PUBS,
    FA2_ITER_REPOS,
    MIN_SEP_AUTHORS,
    MIN_SEP_PUBS,
    NO_DEPT_COLOR,
    NO_DEPT_NAME,
    NO_DEPT_NAME_EN,
    PUB_DEPT_EDGE_K,
    PUB_DEPT_EDGE_WEIGHT,
    PUB_EDGE_MIN_W,
    PUB_LAYOUT_TOP_K,
)
from .layout import (
    dense_rank,
    fa2_blended_layout,
    fit_coords,
    golden_color,
    majority_dept,
    sparse_dept_edges,
    spread_min_distance,
)

logger = logging.getLogger(__name__)


def _initial(value: str) -> str:
    """Первая буква, заглавная, с точкой на конце."""
    return f"{value[0].upper()}."


def _fmt_part(value: str, *, force_initial: bool) -> str:
    """Часть имени-или-отчества, отформатированная для подписи.

    Схлопывается до инициала для публичного показа или всякий раз, когда
    часть стоит рядом с другой частью (заполнены surname_ru/first_name_ru/
    second_name_ru все разом). В остальных случаях оставляется как есть у
    автора — кроме части, которая УЖЕ голый инициал (одна буква, с точкой
    или без — так иногда приходят LLM/каталожные данные): точка ставится
    всегда, это не усечение, а просто верная пунктуация того, что уже
    сказано в данных.
    """
    stripped = value.rstrip(".")
    if force_initial or len(stripped) == 1:
        return _initial(stripped)
    return value


def author_label(
    surname: str | None, first: str | None, second: str | None, *, public: bool = False
) -> str:
    """Один формат на любого автора: сначала фамилия, потом инициалы.

    Берёт три части имени одного языка за раз — собирать подпись на каждом
    языке нужно отдельно из surname_ru/first_name_ru/second_name_ru и
    surname_en/first_name_en/second_name_en. Без отката на сборную сырую
    строку: угадывать фамилию/имя/отчество по порядку слов — ровно тот
    режим отказа, который заменил LLM-шаг в author_names.py, повторять его
    здесь для отображения означало бы вернуть его обратно. Пустая фамилия
    возвращает "" — откат (например, подпись на другом языке или
    свободный текст name_ru) выбирает вызывающий код.
    """
    surname, first, second = surname or "", first or "", second or ""
    if not surname:
        return ""
    if public and len(surname) > 3:
        surname = surname[:3] + ".."
    if first and second:
        return f"{surname} {_fmt_part(first, force_initial=True)}{_fmt_part(second, force_initial=True)}"
    if second:  # уцелело только отчество — считаем его инициалом
        return f"{surname} {_fmt_part(second, force_initial=True)}"
    if first:
        return f"{surname} {_fmt_part(first, force_initial=public)}"
    return surname


def author_variants(row: dict, label_ru: str, label_en: str) -> list[str]:
    """Другие варианты написания имени этого человека, без тех, что уже показаны.

    Карточка уже показывает RU- и EN-подписи и полное русское имя; всё
    остальное, что OpenAlex знает об этом авторе (и полное русское имя,
    когда подпись — только фамилия с инициалами), идёт в свёрнутый список.
    """
    shown = {label_ru.casefold(), label_en.casefold()}
    candidates = [row.get("name_ru") or "", *(row.get("name_variants") or [])]
    variants = []
    for value in candidates:
        cleaned = " ".join((value or "").split())
        if cleaned and cleaned.casefold() not in shown:
            shown.add(cleaned.casefold())
            variants.append(cleaned)
    return variants


@dataclass(frozen=True)
class Authorship:
    """Кто с кем: индекс авторства и публикации, отфильтрованные до тех,
    у кого есть хотя бы один ИТМО-автор."""

    pub_authors: dict[str, list[str]]
    """Публикация -> список её ИТМО-авторов."""
    author_pubs: dict[str, list[str]]
    """Автор -> список его публикаций (тех, что попали в pub_authors)."""
    pubs_rows: list[dict]
    """Строки db["publications"], отфильтрованные до pub_ids."""
    pub_ids: set[str]
    """id публикаций с хотя бы одним ИТМО-автором."""


def _index_authorship(db: dict[str, list[dict]]) -> Authorship:
    """Строит индекс авторства и отсеивает публикации без ИТМО-авторов.

    Аргументы:
        db: Снепшот графа в форме `new_cache::load_db()`.

    Возвращает:
        `Authorship` с индексами в обе стороны и отфильтрованным списком публикаций.
    """
    pub_authors: dict[str, list[str]] = defaultdict(list)
    author_pubs: dict[str, list[str]] = defaultdict(list)
    for row in db["authorship"]:
        pid, per = row["pid"], row["per"]
        pub_authors[pid].append(per)
        author_pubs[per].append(pid)

    pubs_rows = [r for r in db["publications"] if r["id"] in pub_authors]
    pub_ids = {r["id"] for r in pubs_rows}
    logger.info("Публикаций с ИТМО-авторами: %d из %d", len(pubs_rows), len(db["publications"]))
    return Authorship(dict(pub_authors), dict(author_pubs), pubs_rows, pub_ids)


@dataclass(frozen=True)
class DepartmentAssignment:
    """Департамент каждого автора/публикации/репозитория — правила см. `docs/architecture/gui.md`:
    публикация — большинство голосов среди её ИТМО-авторов; автор — департамент
    его самой свежей публикации; репозиторий — большинство по департаментам
    публикаций, которые он реализует. Ничьи — по id, не по глобальной
    популярности (иначе крупные департаменты только росли бы сами от себя)."""

    static_depts: dict[str, list[str]]
    """Автор -> список департаментов, которым он реально принадлежит (BELONGS_TO)."""
    pub_dept_rows: dict[str, list[str]]
    """Публикация -> полный список департаментов (PRODUCED_BY), не только основной."""
    pub_primary: dict[str, str | None]
    """Публикация -> основной департамент (большинство голосов авторов, откат на PRODUCED_BY)."""
    author_dept: dict[str, str | None]
    """Автор -> департамент его самой свежей публикации."""
    repo_pub_map: dict[str, list[str]]
    """Репозиторий -> публикации, которые он реализует (в пределах pub_ids)."""
    repo_dept_rows: dict[str, list[str]]
    """Репозиторий -> полный список департаментов (DEVELOPED_BY)."""
    repo_dept: dict[str, str | None]
    """Репозиторий -> основной департамент."""


def _assign_departments(db: dict[str, list[dict]], dept_name: dict[str, str], authorship: Authorship) -> DepartmentAssignment:
    """Назначает департамент каждому автору/публикации/репозиторию.

    Аргументы:
        db: Снепшот графа.
        dept_name: id департамента -> отображаемое имя (используется только
            как фильтр "департамент существует", не для самого имени).
        authorship: Результат `_index_authorship()`.

    Возвращает:
        `DepartmentAssignment` со всеми промежуточными и итоговыми назначениями.
    """
    static_depts: dict[str, list[str]] = {row["id"]: [] for row in db["persons"]}
    for row in db["person_depts"]:
        per, did = row["per"], row["did"]
        if did in dept_name:
            static_depts[per].append(did)

    pub_dept_rows: dict[str, list[str]] = defaultdict(list)
    for row in db["pub_depts"]:
        pid, did = row["pid"], row["did"]
        if pid in authorship.pub_ids and did in dept_name and did not in pub_dept_rows[pid]:
            pub_dept_rows[pid].append(did)

    pub_primary: dict[str, str | None] = {}
    for pid in authorship.pub_ids:
        primary = majority_dept(static_depts.get(per, []) for per in authorship.pub_authors[pid])
        if primary is None and pub_dept_rows.get(pid):
            primary = pub_dept_rows[pid][0]
        pub_primary[pid] = primary

    pub_date = {r["id"]: (r["publication_date"] or "") for r in authorship.pubs_rows}
    author_dept: dict[str, str | None] = {}
    for per in static_depts:
        dept = None
        for pid in sorted(authorship.author_pubs.get(per, []), key=lambda p: (pub_date.get(p, ""), p), reverse=True):
            if pub_primary.get(pid):
                dept = pub_primary[pid]
                break
        author_dept[per] = dept

    repo_pub_map: dict[str, list[str]] = defaultdict(list)
    for row in db["repo_pubs"]:
        rid, pid = row["rid"], row["pid"]
        if pid in authorship.pub_ids:
            repo_pub_map[rid].append(pid)
    repo_dept_rows: dict[str, list[str]] = defaultdict(list)
    for row in db["repo_depts"]:
        rid, did = row["rid"], row["did"]
        if did in dept_name and did not in repo_dept_rows[rid]:
            repo_dept_rows[rid].append(did)

    repo_dept: dict[str, str | None] = {}
    for row in db["repositories"]:
        rid = row["id"]
        primary = majority_dept(
            [dept] for p in repo_pub_map.get(rid, []) if (dept := pub_primary.get(p))
        )
        if primary is None and repo_dept_rows.get(rid):
            primary = repo_dept_rows[rid][0]
        repo_dept[rid] = primary

    return DepartmentAssignment(
        static_depts=static_depts,
        pub_dept_rows=dict(pub_dept_rows),
        pub_primary=pub_primary,
        author_dept=author_dept,
        repo_pub_map=dict(repo_pub_map),
        repo_dept_rows=dict(repo_dept_rows),
        repo_dept=repo_dept,
    )


@dataclass(frozen=True)
class DepartmentTable:
    """Итоговая таблица департаментов для карты плюс функция сжатия id."""

    departments: list[dict]
    """Записи для `graph-data.json["departments"]`, отсортированы по размеру, плюс "без департамента" последней."""
    g: Callable[[str | None], int]
    """id департамента графа -> плотный id для фронтенда (или id "без департамента", если department is falsy)."""
    no_dept_gid: int
    """Плотный id корзины "без департамента"."""


def _build_department_table(
    dept_name: dict[str, str],
    dept_name_en: dict[str, str],
    assignment: DepartmentAssignment,
    authorship: Authorship,
    db: dict[str, list[dict]],
) -> DepartmentTable:
    """Считает, сколько сущностей у каждого департамента, сортирует по
    размеру и переиндексирует в плотные id 0..N (плюс отдельная корзина
    "без департамента" последней) — так фронтенду не нужно знать реальные
    (произвольные) id департаментов графа.

    Аргументы:
        dept_name: id департамента -> русское/английское имя.
        dept_name_en: id департамента -> английское имя.
        assignment: Результат `_assign_departments()`.
        authorship: Результат `_index_authorship()`.
        db: Снепшот графа (нужен для полного списка репозиториев).

    Возвращает:
        `DepartmentTable`.
    """
    usage: Counter[str] = Counter()
    for d in assignment.author_dept.values():
        if d:
            usage[d] += 1
    for d in assignment.pub_primary.values():
        if d:
            usage[d] += 1
    for d in assignment.repo_dept.values():
        if d:
            usage[d] += 1
    # += 0, а не просто "запомнить множество id": департамент, который
    # встречается только как вторичный (PRODUCED_BY/DEVELOPED_BY), а ничьим
    # основным не бывает, всё равно обязан попасть ключом в usage — иначе
    # его не будет в gid ниже, и table.g() упадёт с KeyError при первом же
    # обращении к нему как к неосновному департаменту публикации/репозитория.
    for rows in (assignment.pub_dept_rows, assignment.repo_dept_rows):
        for depts in rows.values():
            for d in depts:
                usage[d] += 0

    ordered = sorted(usage, key=lambda d: (-usage[d], dept_name[d]))
    gid = {d: i for i, d in enumerate(ordered)}
    no_dept_gid = len(ordered)

    def g(dept_db_id: str | None) -> int:
        return gid[dept_db_id] if dept_db_id else no_dept_gid

    n_auth = Counter(g(d) for d in assignment.author_dept.values())
    n_pub = Counter(g(assignment.pub_primary[p]) for p in authorship.pub_ids)
    n_repo = Counter(g(assignment.repo_dept[r["id"]]) for r in db["repositories"])

    departments = [
        {
            "id": gid[d],
            "name": dept_name[d],
            "name_en": dept_name_en[d],
            "color": golden_color(gid[d]),
            "n": n_auth[gid[d]] + n_pub[gid[d]] + n_repo[gid[d]],
            "n_authors": n_auth[gid[d]],
            "n_pubs": n_pub[gid[d]],
            "n_repos": n_repo[gid[d]],
        }
        for d in ordered
    ]
    departments.append(
        {
            "id": no_dept_gid,
            "name": NO_DEPT_NAME,
            "name_en": NO_DEPT_NAME_EN,
            "color": NO_DEPT_COLOR,
            "n": n_auth[no_dept_gid] + n_pub[no_dept_gid] + n_repo[no_dept_gid],
            "n_authors": n_auth[no_dept_gid],
            "n_pubs": n_pub[no_dept_gid],
            "n_repos": n_repo[no_dept_gid],
        }
    )
    logger.info('Департаментов: %d (+ "%s")', len(ordered), NO_DEPT_NAME)
    return DepartmentTable(departments=departments, g=g, no_dept_gid=no_dept_gid)


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
    pos_pubs: dict[str, tuple[float, float]]
    pos_repos: dict[str, tuple[float, float]]
    coauth: Counter[tuple[str, str]]
    """Пары авторов -> число совместных публикаций (настоящее, для coauth_edges)."""
    pub_pair_w: Counter[tuple[str, str]]
    """Пары публикаций -> число общих ИТМО-авторов (настоящее, для pub_edges)."""
    repo_edge_w: dict[tuple[str, str], int]
    """Пары репозиториев -> число общих публикаций (используется и для раскладки, и для repo_edges — здесь раздвоения нет)."""


def _layout_graph(db: dict[str, list[dict]], authorship: Authorship, assignment: DepartmentAssignment, seed: int) -> Layout:
    """Считает три раскладки ForceAtlas2 (авторы/публикации/репозитории) —
    у каждой своя мера близости, см. `docs/architecture/gui.md`.

    Аргументы:
        db: Снепшот графа.
        authorship: Результат `_index_authorship()`.
        assignment: Результат `_assign_departments()`.
        seed: Сид ForceAtlas2 и подмешивания несвязанных компонент.

    Возвращает:
        `Layout` с позициями и весами рёбер для экспорта.
    """
    rng = random.Random(seed)

    # --- авторы: совместные публикации + общие репозитории + разреженные
    # "тот же департамент"-рёбра. Первое (coauth) уходит и в раскладку, и
    # в экспорт как есть; репозитории и dept-рёбра — только в раскладку.
    coauth: Counter[tuple[str, str]] = Counter()
    for _pid, pers in authorship.pub_authors.items():
        for a, b in combinations(sorted(set(pers)), 2):
            coauth[(a, b)] += 1

    # Обычный dict, а не Counter: дальше в него подмешиваются дробные веса
    # dept-рёбер (sparse_dept_edges), а Counter в typeshed типизирован
    # только под int — тот же приём, что и у pub_layout_w ниже.
    author_layout_w: dict[tuple[str, str], float] = dict(coauth)
    repo_contributors: dict[str, set[str]] = defaultdict(set)
    for row in db["repo_persons"]:
        repo_contributors[row["rid"]].add(row["per"])
    for pers in repo_contributors.values():
        for a, b in combinations(sorted(pers), 2):
            author_layout_w[(a, b)] = author_layout_w.get((a, b), 0) + 1
    for pair, w in sparse_dept_edges(set(assignment.static_depts), assignment.author_dept, rng).items():
        author_layout_w[pair] = author_layout_w.get(pair, 0) + w

    t0 = time.time()
    pos_authors, (n_giant, e_giant, n_small, n_single) = fa2_blended_layout(
        author_layout_w, set(assignment.static_depts), FA2_ITER_AUTHORS, seed
    )
    pos_authors = spread_min_distance(pos_authors, MIN_SEP_AUTHORS, seed)
    logger.info(
        "FA2 по авторам: гигант %d узлов / %d рёбер, подмешано: %d маленьких компонент + %d синглтонов, "
        "min-sep %.1f, %.1f с",
        n_giant, e_giant, n_small, n_single, MIN_SEP_AUTHORS, time.time() - t0,
    )

    # --- публикации: общие ИТМО-авторы. Полный граф w>=1 — тысячи рёбер,
    # поэтому для раскладки берётся top-K сильнейших связей на публикацию;
    # на экспорт (pub_edges) идёт полный pub_pair_w, не урезанный.
    pub_pair_w: Counter[tuple[str, str]] = Counter()
    for _per, plist in authorship.author_pubs.items():
        for a, b in combinations(sorted(set(plist)), 2):
            pub_pair_w[(a, b)] += 1

    t0 = time.time()
    strongest: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for (a, b), w in pub_pair_w.items():
        strongest[a].append((w, b))
        strongest[b].append((w, a))
    pub_layout_w: dict[tuple[str, str], float] = {}
    for n, lst in strongest.items():
        lst.sort(key=lambda t: (-t[0], t[1]))
        for w, o in lst[:PUB_LAYOUT_TOP_K]:
            pub_layout_w[(n, o) if n < o else (o, n)] = w
    for pair, w in sparse_dept_edges(
        authorship.pub_ids, assignment.pub_primary, rng, k=PUB_DEPT_EDGE_K, weight=PUB_DEPT_EDGE_WEIGHT, taper_size=150
    ).items():
        pub_layout_w[pair] = pub_layout_w.get(pair, 0) + w

    pos_pubs, (n_giant_p, e_giant_p, n_small_p, n_single_p) = fa2_blended_layout(
        pub_layout_w, authorship.pub_ids, FA2_ITER_PUBS, seed
    )
    pos_pubs = spread_min_distance(pos_pubs, MIN_SEP_PUBS, seed)
    logger.info(
        "FA2 по публикациям: гигант %d узлов / %d рёбер, подмешано: %d маленьких компонент + %d синглтонов, "
        "min-sep %.1f, %.1f с",
        n_giant_p, e_giant_p, n_small_p, n_single_p, MIN_SEP_PUBS, time.time() - t0,
    )

    # --- репозитории: общие публикации (включая публикации вне графа, у
    # которых нет ни одного ИТМО-автора — репозиторий их всё равно реализует).
    repo_all_pubs: dict[str, set[str]] = defaultdict(set)
    for row in db["repo_pubs"]:
        repo_all_pubs[row["rid"]].add(row["pid"])
    repo_edge_w: dict[tuple[str, str], int] = {}
    for a, b in combinations(sorted(repo_all_pubs), 2):
        shared = len(repo_all_pubs[a] & repo_all_pubs[b])
        if shared:
            repo_edge_w[(a, b)] = shared

    R = nx.Graph()
    R.add_nodes_from(r["id"] for r in db["repositories"])
    R.add_weighted_edges_from((a, b, w) for (a, b), w in repo_edge_w.items())
    pos_repos = fit_coords(nx.forceatlas2_layout(R, max_iter=FA2_ITER_REPOS, weight="weight", seed=seed))  # type: ignore[arg-type]

    return Layout(pos_authors=pos_authors, pos_pubs=pos_pubs, pos_repos=pos_repos, coauth=coauth, pub_pair_w=pub_pair_w, repo_edge_w=repo_edge_w)


def _build_author_nodes(
    db: dict[str, list[dict]], authorship: Authorship, assignment: DepartmentAssignment, table: DepartmentTable, layout: Layout, *, public: bool
) -> tuple[list[dict], list[dict]]:
    """Строит записи авторов сразу в двух формах.

    Аргументы:
        db, authorship, assignment, table, layout: см. соответствующие функции выше.
        public: При `True` detail-записи не строятся вовсе (личные поля не
            существуют вне detail-файла — сборка для GitHub Pages просто
            не производит `authors-detail.json`).

    Возвращает:
        `(summary, detail)` — `summary` идёт в `graph-data.json["authors"]`,
        `detail` (пустой список при `public=True`) — в `authors-detail.json`.
    """
    pubs_count = {per: len(set(authorship.author_pubs.get(per, []))) for per in assignment.static_depts}
    rank_a = dense_rank(pubs_count)
    summary: list[dict] = []
    detail: list[dict] = []
    for row in db["persons"]:
        pid_ = row["id"]
        x, y = layout.pos_authors[pid_]
        label_ru = author_label(row["surname_ru"], row["first_name_ru"], row["second_name_ru"], public=public) or row.get("name_ru") or ""
        label_en = author_label(row["surname_en"], row["first_name_en"], row["second_name_en"], public=public) or label_ru
        summary.append(
            {
                "key": pid_,
                "kind": "author",
                "dept": table.g(assignment.author_dept[pid_]),
                "label": label_ru,
                "label_en": label_en,
                "pubs_count": pubs_count[pid_],
                "rank": rank_a[pid_],
                "gx": x,
                "gy": y,
            }
        )
        if not public:
            detail.append(
                {
                    "key": pid_,
                    "name_ru": row.get("name_ru") or "",
                    "name_variants": author_variants(row, label_ru, label_en),
                    "degree": row["degree"] or "",
                    "github": row["github"] or "",
                    "orcid": row.get("orcid") or "",
                }
            )
    return summary, detail


def _build_repo_nodes(
    db: dict[str, list[dict]], assignment: DepartmentAssignment, table: DepartmentTable, layout: Layout
) -> tuple[list[dict], list[dict]]:
    """Строит записи репозиториев сразу в двух формах (summary/detail).

    Возвращает:
        `(summary, detail)` — `summary` в `graph-data.json["repos"]`,
        `detail` в `repos-detail.json`.
    """
    stars = {r["id"]: (r["stars_num"] or 0) for r in db["repositories"]}
    rank_r = dense_rank(stars)
    summary: list[dict] = []
    detail: list[dict] = []
    for row in db["repositories"]:
        rid = row["id"]
        x, y = layout.pos_repos[rid]
        summary.append(
            {
                "key": rid,
                "kind": "repo",
                "dept": table.g(assignment.repo_dept[rid]),
                "label": row["name"] or "",
                "stars": row["stars_num"] or 0,
                "rank": rank_r[rid],
                "gx": x,
                "gy": y,
            }
        )
        detail.append(
            {
                "key": rid,
                "description": row["description"] or "",
                "owner": row["owner"] or "",
                "url": row["url"] or "",
            }
        )
    return summary, detail


# Единственное место использования (усечение заголовка публикации в
# detail) — локальная константа рядом с функцией, а не в config.py, по
# тому же принципу, что и STRANDED_JITTER в layout.py.
PUB_TITLE_MAX_LEN = 200


def _build_pub_nodes(
    authorship: Authorship, assignment: DepartmentAssignment, table: DepartmentTable, layout: Layout
) -> tuple[list[dict], list[dict]]:
    """Строит записи публикаций сразу в двух формах (summary/detail).

    Detail-часть — то, что раньше строил отдельный `build_search_detail()`
    под `graph-search.js`: заголовок, журнал, DOI, код. Отличие от
    оригинала — код возвращает список ссылок как есть (уже список в
    `new_cache`), разбор `code_url` из JSON-строки — забота потребителя
    снепшота уровнем выше, не этой функции.

    Возвращает:
        `(summary, detail)` — `summary` в `graph-data.json["pubs"]`,
        `detail` в `pubs-detail.json`.
    """
    n_authors_of = {pid: len(set(authorship.pub_authors[pid])) for pid in authorship.pub_ids}
    rank_p = dense_rank(n_authors_of)
    summary: list[dict] = []
    for row in authorship.pubs_rows:
        pid, year = row["id"], row["year"]
        x, y = layout.pos_pubs[pid]
        depts_all = sorted({table.g(d) for d in assignment.pub_dept_rows.get(pid, [])} | {table.g(assignment.pub_primary[pid])})
        summary.append(
            {
                "key": pid,
                "kind": "pub",
                "dept": table.g(assignment.pub_primary[pid]),
                "depts": depts_all,
                "year": year,
                "n_authors": n_authors_of[pid],
                "rank": rank_p[pid],
                "gx": x,
                "gy": y,
            }
        )

    detail: list[dict] = []
    for row in authorship.pubs_rows:
        title = row["title"] or ""
        if len(title) > PUB_TITLE_MAX_LEN:
            title = title[: PUB_TITLE_MAX_LEN - 1] + "…"
        code_url = row["code_url"]
        try:
            urls = json.loads(code_url) if code_url else []
        except (json.JSONDecodeError, TypeError):
            urls = []
        detail.append(
            {
                "key": row["id"],
                "label": title,
                "journal": row["journal"] or "",
                "doi": row["doi"] or "",
                "has_code": bool(row["has_code"]),
                "code_url": urls if isinstance(urls, list) else [urls],
            }
        )
    return summary, detail


def _build_edges(
    db: dict[str, list[dict]], authorship: Authorship, assignment: DepartmentAssignment, table: DepartmentTable, layout: Layout
) -> dict[str, list[dict]]:
    """Строит все семь типов рёбер для `graph-data.json`.

    Возвращает:
        Словарь с ключами `coauth_edges`/`pub_edges`/`repo_edges`/
        `dept_edges`/`repo_author_edges`/`repo_pub_edges`/`all_edges`.
    """
    coauth_edges = [{"s": a, "t": b, "w": w} for (a, b), w in layout.coauth.items() if w >= COAUTH_MIN_W]
    pub_edges = [{"s": a, "t": b, "w": w} for (a, b), w in layout.pub_pair_w.items() if w >= PUB_EDGE_MIN_W]
    repo_edges = [{"s": a, "t": b, "w": w} for (a, b), w in layout.repo_edge_w.items()]

    dept_pair_w: Counter[tuple[int, int]] = Counter()
    for pid in authorship.pub_ids:
        ds = sorted({table.g(assignment.author_dept[per]) for per in authorship.pub_authors[pid]} - {table.no_dept_gid})
        for a, b in combinations(ds, 2):
            dept_pair_w[(a, b)] += 1
    dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

    repo_author_edges = [
        {"s": row["rid"], "t": row["per"], "role": row["role"]}
        for row in db["repo_persons"]
        if row["per"] in assignment.static_depts
    ]
    repo_pub_edges = [
        {"s": row["rid"], "t": row["pid"]} for row in db["repo_pubs"] if row["pid"] in authorship.pub_ids
    ]
    all_edges = [{"s": row["per"], "t": row["pid"]} for row in db["authorship"]]

    logger.info(
        "Рёбра: coauth %d, pub %d, repo %d, dept %d, repo-author %d, repo-pub %d, authorship %d",
        len(coauth_edges), len(pub_edges), len(repo_edges), len(dept_edges), len(repo_author_edges), len(repo_pub_edges), len(all_edges),
    )
    return {
        "coauth_edges": coauth_edges,
        "pub_edges": pub_edges,
        "repo_edges": repo_edges,
        "dept_edges": dept_edges,
        "repo_author_edges": repo_author_edges,
        "repo_pub_edges": repo_pub_edges,
        "all_edges": all_edges,
    }


def build_graph_data(db: dict[str, list[dict]], seed: int, *, public: bool = False) -> tuple[dict, dict[str, list[dict]]]:
    """Собирает граф целиком: раскладка + узлы + рёбра, из снепшота `new_cache`.

    Аргументы:
        db: Снепшот графа в форме `new_cache::load_db()`.
        seed: Сид ForceAtlas2 (для воспроизводимости раскладки).
        public: Собирать ли публичную (GitHub Pages) сборку — при `True`
            detail-файл авторов не строится вовсе (см. `_build_author_nodes`).

    Возвращает:
        `(summary, detail)`:
        - `summary` — то, что пишется в `graph-data.json` (department table,
          все рёбра, summary-записи узлов);
        - `detail` — словарь `{"authors": [...], "repos": [...], "pubs": [...]}`,
          каждый список пишется в свой `*-detail.json`.
    """
    dept_name = {row["id"]: (row["name_ru"] or row["name_en"] or "") for row in db["departments"]}
    dept_name_en = {row["id"]: (row["name_en"] or "") for row in db["departments"]}

    authorship = _index_authorship(db)
    assignment = _assign_departments(db, dept_name, authorship)
    table = _build_department_table(dept_name, dept_name_en, assignment, authorship, db)
    layout = _layout_graph(db, authorship, assignment, seed=seed)

    authors_summary, authors_detail = _build_author_nodes(db, authorship, assignment, table, layout, public=public)
    repos_summary, repos_detail = _build_repo_nodes(db, assignment, table, layout)
    pubs_summary, pubs_detail = _build_pub_nodes(authorship, assignment, table, layout)
    edges = _build_edges(db, authorship, assignment, table, layout)

    summary = {
        "departments": table.departments,
        "authors": authors_summary,
        "repos": repos_summary,
        "pubs": pubs_summary,
        **edges,
    }
    detail = {"authors": authors_detail, "repos": repos_detail, "pubs": pubs_detail}
    return summary, detail


def dump_json(data, path: Path) -> None:
    """Пишет данные голым JSON (не `window.X=...;` — эта обёртка была нужна
    только `pauk/gui/web/`, `new_gui` читает JSON напрямую через `fetch`)."""
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    logger.info("Записан %s (%.1f МБ)", path, path.stat().st_size / 1e6)


def main() -> None:
    from new_cache.graph_snapshot import read_snapshot

    data_dir = Path(__file__).resolve().parent / "data"

    parser = argparse.ArgumentParser(description="Генерация статических данных для new_gui")
    parser.add_argument(
        "--public",
        action="store_true",
        help="не строить authors-detail.json - для сборки, покидающей корпоративную сеть (например, GitHub Pages)",
    )
    parser.add_argument("--out-dir", type=Path, default=None, help="куда писать graph-data.json и *-detail.json")
    parser.add_argument("--seed", type=int, default=42, help="сид раскладки FA2")
    parser.add_argument(
        "--cache", type=Path, required=True, help="путь к снепшоту графа, снятому 'new_cache' (не 'pauk cache export')"
    )
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = data_dir / ("public" if args.public else "private")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    db = read_snapshot(args.cache)
    summary, detail = build_graph_data(db, seed=args.seed, public=args.public)

    dump_json(summary, args.out_dir / "graph-data.json")
    for kind, rows in detail.items():
        if rows:
            dump_json(rows, args.out_dir / f"{kind}-detail.json")

    logger.info("Готово за %.1f с", time.time() - t0)


if __name__ == "__main__":
    main()
