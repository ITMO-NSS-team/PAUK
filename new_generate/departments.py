"""Назначение департамента каждому автору/публикации/репозиторию — правила
см. `docs/architecture/gui.md`: публикация — большинство голосов среди её
ИТМО-авторов; автор — департамент его самой свежей публикации; репозиторий —
большинство по департаментам публикаций, которые он реализует. Ничьи — по
id, не по глобальной популярности (иначе крупные департаменты только росли
бы сами от себя).
"""

from __future__ import annotations

import colorsys
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .authorship import Authorship
from .config import NO_DEPT_COLOR, NO_DEPT_NAME, NO_DEPT_NAME_EN

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class DepartmentAssignment:
    """Департамент каждого автора/публикации/репозитория."""

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


@dataclass(frozen=True)
class DepartmentTable:
    """Итоговая таблица департаментов для карты плюс функция сжатия id."""

    departments: list[dict]
    """Записи для `graph-data.json["departments"]`, отсортированы по размеру, плюс "без департамента" последней."""
    g: Callable[[str | None], int]
    """id департамента графа -> плотный id для фронтенда (или id "без департамента", если department is falsy)."""
    no_dept_gid: int
    """Плотный id корзины "без департамента"."""


class DepartmentAssigner:
    """Назначает департамент каждой сущности и строит итоговую таблицу для
    фронтенда — два метода вместо двух свободных функций, потому что оба
    делят один и тот же контекст (`db`/`authorship`) и в graph_builder.py
    вызываются друг за другом ровно один раз за прогон.
    """

    def __init__(self, db: dict[str, list[dict]], authorship: Authorship) -> None:
        self.db = db
        self.authorship = authorship

    def assign(self, dept_name: dict[str, str]) -> DepartmentAssignment:
        """Назначает департамент каждому автору/публикации/репозиторию.

        Аргументы:
            dept_name: id департамента -> отображаемое имя (используется
                только как фильтр "департамент существует", не для самого имени).

        Возвращает:
            `DepartmentAssignment` со всеми промежуточными и итоговыми назначениями.
        """
        db, authorship = self.db, self.authorship

        # Шаг 1: BELONGS_TO как есть — все реальные департаменты каждого
        # автора (может быть несколько, может не быть вовсе). Ключ заводится
        # для КАЖДОГО автора из persons (даже без единой связи) — иначе
        # KeyError ниже, в цикле author_dept, у автора без BELONGS_TO вообще.
        static_depts: dict[str, list[str]] = {row["id"]: [] for row in db["persons"]}
        for row in db["person_depts"]:
            per, did = row["per"], row["did"]
            if did in dept_name:  # молча пропускаем связь на несуществующий/удалённый департамент
                static_depts[per].append(did)

        # Шаг 2: PRODUCED_BY как есть — полный список департаментов публикации,
        # не только основной (тот считается ниже, в pub_primary). Фильтр по
        # pub_ids — публикации без ИТМО-авторов сюда вообще не попадают.
        pub_dept_rows: dict[str, list[str]] = defaultdict(list)
        for row in db["pub_depts"]:
            pid, did = row["pid"], row["did"]
            if pid in authorship.pub_ids and did in dept_name and did not in pub_dept_rows[pid]:
                pub_dept_rows[pid].append(did)

        # Шаг 3: основной департамент публикации — большинство голосов среди
        # её ИТМО-авторов (по их static_depts), а если голосов вообще не было
        # (авторы без единого BELONGS_TO) — откат на первый PRODUCED_BY.
        pub_primary: dict[str, str | None] = {}
        for pid in authorship.pub_ids:
            primary = majority_dept(static_depts.get(per, []) for per in authorship.pub_authors[pid])
            if primary is None and pub_dept_rows.get(pid):
                primary = pub_dept_rows[pid][0]
            pub_primary[pid] = primary

        # Шаг 4: департамент автора — основной департамент его САМОЙ СВЕЖЕЙ
        # публикации (сортировка по дате по убыванию, берём первую, у которой
        # вообще нашёлся pub_primary — старые публикации без департамента
        # пропускаются, а не останавливают поиск).
        pub_date = {r["id"]: (r["publication_date"] or "") for r in authorship.pubs_rows}
        author_dept: dict[str, str | None] = {}
        for per in static_depts:
            dept = None
            for pid in sorted(authorship.author_pubs.get(per, []), key=lambda p: (pub_date.get(p, ""), p), reverse=True):
                if pub_primary.get(pid):
                    dept = pub_primary[pid]
                    break
            author_dept[per] = dept

        # Шаг 5: репозиторий -> публикации, которые он реализует (IMPLEMENTS,
        # см. edges.py), в пределах pub_ids — нужно ниже, чтобы посчитать
        # департамент репозитория через департаменты ЭТИХ публикаций.
        repo_pub_map: dict[str, list[str]] = defaultdict(list)
        for row in db["repo_pubs"]:
            rid, pid = row["rid"], row["pid"]
            if pid in authorship.pub_ids:
                repo_pub_map[rid].append(pid)
        # DEVELOPED_BY как есть — та же роль для репозитория, что PRODUCED_BY
        # для публикации (полный список, не только основной).
        repo_dept_rows: dict[str, list[str]] = defaultdict(list)
        for row in db["repo_depts"]:
            rid, did = row["rid"], row["did"]
            if did in dept_name and did not in repo_dept_rows[rid]:
                repo_dept_rows[rid].append(did)

        # Шаг 6: основной департамент репозитория — большинство голосов среди
        # департаментов публикаций, которые он реализует (через repo_pub_map
        # + pub_primary), откат на DEVELOPED_BY по той же логике, что и у публикаций.
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

    def build_table(
        self, dept_name: dict[str, str], dept_name_en: dict[str, str], assignment: DepartmentAssignment
    ) -> DepartmentTable:
        """Считает, сколько сущностей у каждого департамента, сортирует по
        размеру и переиндексирует в плотные id 0..N (плюс отдельная корзина
        "без департамента" последней) — так фронтенду не нужно знать реальные
        (произвольные) id департаментов графа.

        Аргументы:
            dept_name: id департамента -> русское имя.
            dept_name_en: id департамента -> английское имя.
            assignment: Результат `assign()`.

        Возвращает:
            `DepartmentTable`.
        """
        db, authorship = self.db, self.authorship

        # Считаем, сколько раз каждый департамент оказался ЧЬИМ-ТО основным
        # (author_dept/pub_primary/repo_dept) — от этой суммы зависит порядок
        # сортировки ниже: крупные департаменты получают меньший (более
        # заметный) плотный id.
        usage: Counter[str] = Counter()
        for d in assignment.author_dept.values():
            if d:  # None — "у сущности нет департамента", не считаем как голос
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

        # Сортировка по убыванию usage, ничьи — по имени (детерминированно,
        # не по случайному порядку словаря). gid — плотные id 0..N-1 в этом
        # порядке; no_dept_gid — следующий id сразу за последним настоящим
        # департаментом, под корзину "без департамента".
        ordered = sorted(usage, key=lambda d: (-usage[d], dept_name[d]))
        gid = {d: i for i, d in enumerate(ordered)}
        no_dept_gid = len(ordered)

        def g(dept_db_id: str | None) -> int:
            # Falsy (None или "") -> корзина "без департамента", а не KeyError.
            return gid[dept_db_id] if dept_db_id else no_dept_gid

        # Считаем количество авторов/публикаций/репозиториев на КАЖДЫЙ плотный
        # id разом (через Counter), а не по одному через отдельные циклы —
        # эти три числа идут прямо в поля n_authors/n_pubs/n_repos ниже.
        n_auth = Counter(g(d) for d in assignment.author_dept.values())
        n_pub = Counter(g(assignment.pub_primary[p]) for p in authorship.pub_ids)
        n_repo = Counter(g(assignment.repo_dept[r["id"]]) for r in db["repositories"])

        # Одна запись на каждый настоящий департамент, в отсортированном
        # порядке (ordered), с плотным id/цветом/тремя видами счётчиков.
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
        # И одна дополнительная запись — синтетическая корзина "без
        # департамента" (id/имя/цвет — из config.py, не из реальных
        # департаментов графа), всегда последней в списке.
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
