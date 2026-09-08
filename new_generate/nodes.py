"""Сборка узлов трёх видов (авторы/репозитории/публикации) сразу в двух
формах — "summary" (то, что нужно нарисовать точку на карте) и "detail"
(расширенные поля, которые new_gui подгружает лениво после карты).
"""

from __future__ import annotations

import json

from .authorship import Authorship
from .departments import DepartmentAssignment, DepartmentTable


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


# Единственное место использования (усечение заголовка публикации в
# detail) — локальная константа рядом с классом, а не в config.py, по
# тому же принципу, что и STRANDED_JITTER в layout.py.
PUB_TITLE_MAX_LEN = 200


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


def author_variants(row: dict, label_ru: str, label_en: str) -> dict[str, list[str]]:
    """Другие варианты написания имени этого человека, без тех, что уже
    показаны как заголовок карточки — раздельно по источнику.

    Заголовок приватной карточки (`new_gui/src/features/panels.ts`) — это
    сокращённая подпись (`label`/`label_en`) ПОКА detail не домержился, а
    как только домержился — `name_ru`/`name_en` целиком (полное имя вместо
    "Фамилия И.О."). Раз `name_ru`/`name_en` сами становятся заголовком,
    здесь они исключены из кандидатов в свёрнутый список — иначе то же имя
    показывалось бы дважды. Источники, которые в этот список всё же идут:
    `name_variants` — то, что OpenAlex видел по разным публикациям автора;
    `other_names` — имя, под которым автор сам просит его указывать
    (ORCID credit-name), плюс варианты, которые он сам зарегистрировал в
    своём профиле. Разные по происхождению вещи, поэтому не сливаются в
    один список — это решает карточка (см. `field.nameVariantsOpenAlex`/
    `field.nameVariantsOrcid` в `new_gui/src/core/i18n.ts`).
    """
    shown = {
        label_ru.casefold(),
        label_en.casefold(),
        (row.get("name_ru") or "").casefold(),
        (row.get("name_en") or "").casefold(),
    }

    def dedup(values: list[str]) -> list[str]:
        result = []
        for value in values:
            cleaned = " ".join((value or "").split())
            if cleaned and cleaned.casefold() not in shown:
                shown.add(cleaned.casefold())
                result.append(cleaned)
        return result

    return {
        "openalex": dedup(row.get("name_variants") or []),
        "orcid": dedup(row.get("other_names") or []),
    }


class AuthorNodeBuilder:
    """Строит записи авторов сразу в двух формах — держит контекст, общий
    для каждой строки (assignment/table/positions/public), вместо того
    чтобы протаскивать его параметром в свободную функцию."""

    def __init__(
        self,
        db: dict[str, list[dict]],
        authorship: Authorship,
        assignment: DepartmentAssignment,
        table: DepartmentTable,
        pos: dict[str, tuple[float, float]],
        *,
        public: bool,
    ) -> None:
        self.db = db
        self.authorship = authorship
        self.assignment = assignment
        self.table = table
        self.pos = pos
        self.public = public

    def build(self) -> tuple[list[dict], list[dict]]:
        """Возвращает:
            `(summary, detail)` — `summary` идёт в `graph-data.json["authors"]`,
            `detail` (пустой список при `public=True`) — в `authors-detail.json`,
            личные поля не существуют вне detail-файла.
        """
        pubs_count = {per: len(set(self.authorship.author_pubs.get(per, []))) for per in self.assignment.static_depts}
        rank_a = dense_rank(pubs_count)
        summary: list[dict] = []
        detail: list[dict] = []
        for row in self.db["persons"]:
            pid_ = row["id"]
            x, y = self.pos[pid_]
            label_ru = author_label(row["surname_ru"], row["first_name_ru"], row["second_name_ru"], public=self.public) or row.get("name_ru") or ""
            label_en = author_label(row["surname_en"], row["first_name_en"], row["second_name_en"], public=self.public) or label_ru
            summary.append(
                {
                    "key": pid_,
                    "kind": "author",
                    "dept": self.table.g(self.assignment.author_dept[pid_]),
                    "label": label_ru,
                    "label_en": label_en,
                    "pubs_count": pubs_count[pid_],
                    "rank": rank_a[pid_],
                    "gx": x,
                    "gy": y,
                }
            )
            if not self.public:
                detail.append(
                    {
                        "key": pid_,
                        "name_ru": row.get("name_ru") or "",
                        "name_en": row.get("name_en") or "",
                        "name_variants": author_variants(row, label_ru, label_en),
                        "degree": row["degree"] or "",
                        "github": row["github"] or "",
                        "orcid": row.get("orcid") or "",
                    }
                )
        return summary, detail


class RepoNodeBuilder:
    """Строит записи репозиториев сразу в двух формах (summary/detail)."""

    def __init__(
        self,
        db: dict[str, list[dict]],
        assignment: DepartmentAssignment,
        table: DepartmentTable,
        pos: dict[str, tuple[float, float]],
    ) -> None:
        self.db = db
        self.assignment = assignment
        self.table = table
        self.pos = pos

    def build(self) -> tuple[list[dict], list[dict]]:
        """Возвращает:
            `(summary, detail)` — `summary` в `graph-data.json["repos"]`,
            `detail` в `repos-detail.json`.
        """
        stars = {r["id"]: (r["stars_num"] or 0) for r in self.db["repositories"]}
        rank_r = dense_rank(stars)
        summary: list[dict] = []
        detail: list[dict] = []
        for row in self.db["repositories"]:
            rid = row["id"]
            x, y = self.pos[rid]
            summary.append(
                {
                    "key": rid,
                    "kind": "repo",
                    "dept": self.table.g(self.assignment.repo_dept[rid]),
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
                    "url": row["url"] or "",
                }
            )
        return summary, detail


class PubNodeBuilder:
    """Строит записи публикаций сразу в двух формах (summary/detail).

    Detail-часть — то, что раньше строил отдельный `build_search_detail()`
    под `graph-search.js`: заголовок, журнал, DOI, код. Отличие от
    оригинала — код возвращает список ссылок как есть (уже список в
    `new_cache`), разбор `code_url` из JSON-строки — забота этого класса,
    не потребителя снепшота уровнем выше.
    """

    def __init__(
        self,
        authorship: Authorship,
        assignment: DepartmentAssignment,
        table: DepartmentTable,
        pos: dict[str, tuple[float, float]],
    ) -> None:
        self.authorship = authorship
        self.assignment = assignment
        self.table = table
        self.pos = pos

    def build(self) -> tuple[list[dict], list[dict]]:
        """Возвращает:
            `(summary, detail)` — `summary` в `graph-data.json["pubs"]`,
            `detail` в `pubs-detail.json`.
        """
        n_authors_of = {pid: len(set(self.authorship.pub_authors[pid])) for pid in self.authorship.pub_ids}
        rank_p = dense_rank(n_authors_of)
        summary: list[dict] = []
        for row in self.authorship.pubs_rows:
            pid, year = row["id"], row["year"]
            x, y = self.pos[pid]
            depts_all = sorted(
                {self.table.g(d) for d in self.assignment.pub_dept_rows.get(pid, [])}
                | {self.table.g(self.assignment.pub_primary[pid])}
            )
            summary.append(
                {
                    "key": pid,
                    "kind": "pub",
                    "dept": self.table.g(self.assignment.pub_primary[pid]),
                    "depts": depts_all,
                    "year": year,
                    "n_authors": n_authors_of[pid],
                    "rank": rank_p[pid],
                    "gx": x,
                    "gy": y,
                }
            )

        detail: list[dict] = []
        for row in self.authorship.pubs_rows:
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
