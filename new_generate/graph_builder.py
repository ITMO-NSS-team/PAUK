"""Снепшот `new_cache` -> раскладка -> JSON для сайта — точка входа и
оркестрация. Сама логика по стадиям разложена по модулям (`authorship.py`,
`departments.py`, `layout.py`, `nodes.py`, `edges.py`) — этот файл только
собирает их в правильном порядке и отвечает за CLI/запись на диск.

- `db` — в форме `new_cache::load_db()`: списки словарей (`cypher_dict`
  для персон/публикаций/репозиториев), а не смесь словарей и позиционных
  кортежей, как в оригинальном `pauk/gui/generate_data.py`.
- Узлы строятся сразу в двух формах — "summary" (то, что нужно нарисовать
  точку на карте: `key`/`kind`/`dept`/`label`/`rank`/`gx`/`gy` плюс одна
  сводная цифра) и "detail" (всё остальное — расширенные поля, которые
  `new_gui` подгружает лениво после карты). Раньше так уже было сделано
  ровно для одной сущности (публикации — отдельный
  `build_search_detail()`/`graph-search.js`); здесь это обобщено на все
  четыре типа, а не изобретено заново.
- Департаменты пока БЕЗ отдельного detail-файла: сегодня в departments
  нет ни одного поля, которого не было бы уже в summary.
- Вывод — голый JSON (не `window.GRAPH=...;`): `new_gui` уже умеет читать
  голый JSON (`core/data.ts::loadGraphData()`), обёртка была нужна только
  старому `pauk/gui/web/`, который сюда не относится.
- `--public`-сборка (для GitHub Pages) не вырезает отдельные поля из
  объекта автора, а просто не пишет `authors-detail.json` вовсе — надёжнее,
  чем список полей на вырезание: забыть добавить в него новое личное поле
  сейчас невозможно в принципе, потому что личные поля физически не
  существуют вне detail-файла.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from .authorship import build_authorship_index
from .departments import DepartmentAssigner
from .edges import EdgeBuilder
from .layout import GraphLayoutBuilder
from .nodes import AuthorNodeBuilder, PubNodeBuilder, RepoNodeBuilder

logger = logging.getLogger(__name__)


class GraphDataBuilder:
    """Собирает граф целиком: раскладка + узлы + рёбра, из снепшота
    `new_cache` — держит `db`/`seed`/`public` как состояние, `build()`
    вызывается ровно один раз за прогон.
    """

    def __init__(self, db: dict[str, list[dict]], seed: int, *, public: bool = False) -> None:
        """Сохраняет входные данные — сама сборка происходит только в `build()`.

        Аргументы:
            db: Снепшот графа в форме `new_cache::load_db()`.
            seed: Сид ForceAtlas2 (для воспроизводимости раскладки).
            public: Собирать ли публичную (GitHub Pages) сборку — при
                `True` detail-файл авторов не строится вовсе.
        """
        self.db = db
        self.seed = seed
        self.public = public

    def build(self) -> tuple[dict, dict[str, list[dict]]]:
        """Собирает граф целиком: раскладка + узлы + рёбра, из снепшота `new_cache`.

        Возвращает:
            `(summary, detail)`:
            - `summary` — то, что пишется в `graph-data.json` (department
              table, все рёбра, summary-записи узлов);
            - `detail` — словарь `{"authors": [...], "repos": [...], "pubs": [...]}`,
              каждый список пишется в свой `*-detail.json`.
        """
        db = self.db
        # dept_name — русское имя ИЛИ английское как запасной вариант (нужно,
        # только чтобы department точно "существовал" — фильтр в
        # DepartmentAssigner.assign(), не для отображения); dept_name_en —
        # отдельно, чисто английское, идёт напрямую в итоговую таблицу.
        dept_name = {row["id"]: (row["name_ru"] or row["name_en"] or "") for row in db["departments"]}
        dept_name_en = {row["id"]: (row["name_en"] or "") for row in db["departments"]}

        # Порядок стадий важен: authorship нужен для assign() (кто чей автор),
        # assignment — и для build_table() (кто в каком департаменте), и для
        # раскладки (dept-рёбра), и для сборки узлов/рёбер ниже.
        authorship = build_authorship_index(db)
        assigner = DepartmentAssigner(db, authorship)
        assignment = assigner.assign(dept_name)
        table = assigner.build_table(dept_name, dept_name_en, assignment)
        layout = GraphLayoutBuilder(db, authorship, assignment).build(self.seed)

        # Три вида узлов собираются независимо друг от друга (свой Builder на
        # каждый), но всем нужны table (для dept/цвета) и своя часть layout
        # (позиции). authors — единственный, кому нужен ещё и public (детали
        # авторов приватные).
        authors_summary, authors_detail = AuthorNodeBuilder(
            db, authorship, assignment, table, layout.pos_authors, public=self.public
        ).build()
        repos_summary, repos_detail = RepoNodeBuilder(db, assignment, table, layout.pos_repos).build()
        pubs_summary, pubs_detail = PubNodeBuilder(authorship, assignment, table, layout.pos_pubs).build()
        edges = EdgeBuilder(db, authorship, assignment, table, layout).build()

        # **edges разворачивает все семь ключей рёбер (coauth_edges/pub_edges/...)
        # прямо на верхний уровень summary — так их видит new_gui, без
        # вложенного объекта "edges" в JSON.
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
    # Импорты внутри функции, а не на уровне модуля: new_cache — соседний
    # пакет этого же репозитория, а pauk.settings — часть основного пакета
    # pauk. Оба нужны только для CLI (main()), а не для build() — который
    # можно вызвать и с уже готовым db в памяти, без похода за настройками.
    from new_cache.graph_snapshot import read_snapshot
    from pauk.settings import settings

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
    # Дефолт вычисляется только если --out-dir не передан явно — так
    # пользователь всегда может перезаписать путь вручную, но по умолчанию
    # данные лягут туда, откуда их заберёт new_gui (см. pauk.settings.gui_dir).
    if args.out_dir is None:
        args.out_dir = settings.gui_dir / ("public" if args.public else "private")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)  # exist_ok — второй прогон в ту же папку не должен падать

    t0 = time.time()
    db = read_snapshot(args.cache)
    summary, detail = GraphDataBuilder(db, seed=args.seed, public=args.public).build()

    dump_json(summary, args.out_dir / "graph-data.json")
    # detail-файл кроме authors/repos/pubs пишется только если в нём реально
    # есть строки — для --public сборки authors_detail пуст (см.
    # AuthorNodeBuilder.build()), и authors-detail.json тогда не появится
    # на диске вовсе, а не будет лежать пустым списком.
    for kind, rows in detail.items():
        if rows:
            dump_json(rows, args.out_dir / f"{kind}-detail.json")

    logger.info("Готово за %.1f с", time.time() - t0)


if __name__ == "__main__":
    main()
