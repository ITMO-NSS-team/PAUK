# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Все правила репозитория (соглашения по тестам, линтер, стиль коммитов,
политика подтверждения git-операций, стабильность лейблов/связей Neo4j,
стиль комментариев, чек-лист «перед тем как сказать готово») — в
[`AGENTS.md`](AGENTS.md). Этот файл добавляет команды и архитектурный
контекст и не дублирует то, что уже есть в `AGENTS.md`.

## Что это за проект

PAUK собирает публикации ИТМО из OpenAlex, обогащает их данными внешних
источников (Crossref, ORCID, GitHub, LLM для классификации), находит в них
ссылки на код и загружает результат в Neo4j. `pauk/gui/` строит из графа
данные для интерактивной карты, сам сайт — TypeScript в `pauk/gui/web/`.
Python-пакет `pauk/`, Python >=3.12, управляется через `uv`.

## Команды

```bash
uv sync                                                     # установка зависимостей

# Тесты (стиль unittest.TestCase, в CI запускаются через pytest как раннер)
uv run python -m unittest discover -s tests/unit            # локально, обычный раннер unittest
uv run --with pytest pytest tests/ -q                       # то, что запускает CI (unit + bench; integration без Docker пропускается)
uv run --with pytest pytest tests/unit/test_normalize.py -q # один файл
uv run --with pytest pytest tests/unit/test_normalize.py::ClassName::test_method -q  # один тест

# Часть тестов лежит рядом с кодом (pauk/cache/tests/, pauk/gui/graph_builder/tests/) —
# ни discover -s tests/unit, ни pytest tests/ их не видят, и CI их тоже не гоняет —
# после правок pauk/cache/ или pauk/gui/ запускать руками:
uv run --with pytest pytest pauk -q

# tests/bench/ — сквозной прогон collect → enrich → граф на синтетической
# «вселенной» (tests/bench/universe.py) с моками API и in-memory Neo4j, без
# сети и БД. unittest discover его не видит — после правок стадий/загрузчика:
uv run --with pytest pytest tests/bench -q

# tests/integration/ — нужен Docker; без него аккуратно пропускается:
uv run --with pytest --with mongomock --with 'testcontainers[neo4j]' pytest tests/integration -q

uv run ruff check <изменённые файлы>                        # линтер, конфиг в pyproject.toml
uv run ruff format <изменённые файлы>                       # форматтер

# Сайт карты (pauk/gui/web/, Vite + Sigma.js)
cd pauk/gui/web && npm install
npm run dev                                                 # данные берёт из data/gui/private/
npm test && npm run typecheck && npm run lint               # vitest, tsc, eslint (в CI не входят)
npm run build                                               # dist/ для деплоя
```

Точка входа CLI — `pauk` (`pauk/cli.py::main`, argparse, без typer/click):

```bash
pauk run     --work <id> | --works-file <файл> | --from <дата> --to <дата> [--name <имя>]
pauk collect --work <id> | --works-file <файл> | --from <дата> --to <дата> [--name <имя>]
pauk normalize --group <group>
pauk enrich [stage] --group <group> [--input <файл> --entity <сущность>] [--force]
pauk publish graph --group <group>
pauk dedup graph
pauk cache export [--output <путь>] [--only persons,repos,...]  # снепшот Neo4j → data/cache/
pauk cache inspect [<снепшот>] [--table T] [--sample N]
pauk gui build [--cache <снепшот>] [--out-dir <dir>] [--seed 42] # снепшот → data/gui/{public,private}/
pauk versions link [--llm] [--force]                            # связи IS_VERSION между публикациями, по всему графу
pauk versions report [--output <путь>]                          # те же пары, только журнал
pauk admin ...    # ручная правка графа, prune/trim, worker, аккаунты панели; см. docs/admin-panel.md
```

Полный справочник команд: [`docs/architecture/cli.md`](docs/architecture/cli.md).
Сквозной прогон «сбор → граф → web» с проверкой на копии БД:
[`docs/full-run.md`](docs/full-run.md). Этапу `author_names` нужен
`data/static/russian_names.csv` (персональные данные, в репозитории нет;
путь можно задать через `PAUK_RUSSIAN_NAMES_FILE`) — без него полный
`enrich` падает.

## Архитектура

Сначала прочитать [`docs/architecture/overview.md`](docs/architecture/overview.md)
— он короткий, и всё ниже — сжатая версия того же самого. У каждого
подпакета своя заметка в `docs/architecture/`; перед тем как трогать
подпакет, читать конкретную заметку, а не полагаться на это резюме.

**Поток данных:**

```
OpenAlex API
  │  pauk collect
MongoDB: raw                                 RawStore, append-only
  │  pauk normalize
MongoDB: publications/persons/...            PreparedStore, 7 коллекций
  │  pauk enrich [stage]                     persons → departments → code_links → link_relevance →
  │                                          emails → repositories → repo_people → dedup →
  │                                          github_match → author_names
MongoDB: publications/persons/...            те же коллекции, обогащённые
  │  pauk publish graph
Neo4j                                        накопление между прогонами, MERGE
  │  pauk dedup graph (по требованию)
Neo4j                                        схлопывает дубли across групп
  │  pauk cache export
data/cache/graph_snapshot_<dd-mm-yyyy>.json  снепшот на диске
  │  pauk gui build
data/gui/{public,private}/*.json             данные для сайта pauk/gui/web/
```

Ключевые вещи, которые не видны просто по структуре файлов:

- **Сущности глобальные, не по группе.** `<group>` (`pauk/storage/naming.py::group_name`,
  например `<дата>__<work_id>` или `<дата>__from_<start>__to_<end>`) — это
  просто метка (`groups: [...]`) на документе Mongo, а не партиция. Одна и
  та же публикация, затронутая двумя пересекающимися прогонами, — один
  документ, обогащённый один раз. Но чтение скоуплено на вызывающую группу
  (`read_rows`/`read_models` фильтруют по `groups`), так что этап не видит
  строки, которые записала другая группа; `get_rows`/`get_models` — лазейка
  для глобального состояния. См. [`docs/architecture/storage.md`](docs/architecture/storage.md).
- **Neo4j — единственное место, где прогоны накапливаются.** Каждый
  `pauk publish graph --group <group>` вливает свою группу через MERGE;
  ничто ниже по потоку не читает Mongo напрямую. Из цепочки GUI к Neo4j
  ходит только `pauk/cache/export.py`; `pauk/gui/` читает готовый снепшот
  с диска.
- **Резюмируемость** — на уровне строки, через `processing: dict[str,
  ProcessingState]` (`_processing` в JSON) на каждой prepared-модели —
  свой статус на каждый enrichment-этап (`not_started`/`completed`/
  `completed_empty`/`not_applicable`/`failed`). `EnrichmentStage.needs_attempt()`
  решает, ретраить ли строку; `--force` это правило обходит.
- **Порядок enrichment-этапов фиксирован** в `pipeline/stages/__init__.py`
  (`ALL_STAGES`) и важен: `link_relevance` идёт после `code_links`,
  `emails` — до `github_match`, `github_match` нужны аккаунты от
  `repo_people` и схлопнутые `dedup`-ом авторства. `social_graph`
  (`OPTIONAL_STAGES`) в обычный прогон не входит: сотни запросов к API,
  осмыслен только после того, как `github_match` подтвердил аккаунты.
  Источник истины по порядку — сам `ALL_STAGES`, а не списки в docs.
- **Дедупликация — на двух уровнях**, теми же функциями принятия решений
  о слиянии, но с разным источником строк: внутри одной группы
  (`pipeline/stages/dedup.py`, всегда часть `enrich`/`run`, без сети) и
  по всему графу (`pauk/graph/dedup.py`, `pauk dedup graph`, вручную,
  ловит дубли из разных групп). Решение о слиянии персон опирается на
  обученную модель (`pauk/graph/person_resolution*.py`, веса в
  `pauk/graph/artifacts/person_resolution_logreg.pkl`) и LLM-планировщик
  (`pauk/pipeline/person_resolution*.py`). Ошибочное слияние откатывается
  через `pauk/graph/unmerge.py`. См.
  [`docs/architecture/pipeline/dedup.md`](docs/architecture/pipeline/dedup.md).
  Публикации сливаются только по DOI (по заголовку — лишь если у одной
  записи DOI нет): препринт, перевод, журнальная версия доклада — это
  отдельные узлы. Их связывает `pauk versions link`
  (`pauk/graph/version_link.py`, правила в `version_candidates.py`, LLM для
  MEDIUM_FUZZY в `version_judge.py`): ребро
  `(:Publication)-[:IS_VERSION {bucket}]->(:Publication)` для каждого
  бакета, mergeable-пары — в очередь `/review`. Рёбра принадлежат команде,
  а не publish, поэтому prune их не трогает (`GRAPH_OWNED_RELATIONSHIPS`).
  См. [`docs/architecture/pipeline/version-candidates.md`](docs/architecture/pipeline/version-candidates.md).
- **GUI: приватность решается папкой, а не содержимым.** `pauk gui build`
  (`pauk/gui/graph_builder/`) пишет `graph-data.json` и `*-detail.json`;
  `authors-detail.json` (персональные поля) — только в `data/gui/private/`,
  остальное — в обе папки. `vite.config.ts::publicDir` смотрит в
  `data/gui/private/`. Карта показывает только публикации хотя бы с одним
  автором из ИТМО; внешние соавторы в ForceAtlas2 не участвуют, их ставят
  в центроид ИТМО-соавторов. Раскладку проверять глазами — метрики не
  отражают, как она выглядит. Подробно: [`docs/architecture/gui.md`](docs/architecture/gui.md).
- **Слои сайта** в `pauk/gui/web/src/`: `contracts` → `core` → `map` →
  `features` → `app`; слой импортирует только из себя и нижележащих, это
  проверяет `eslint-plugin-boundaries`. `contracts/` повторяет форму JSON,
  который пишет `graph_builder` — меняя одно, менять и другое.
- **`pauk admin`** правит граф вручную через `pauk/graph/mutations.py`,
  по белому списку от `NODE_REGISTRY`. `node set`/`node delete` сохраняют
  решение в `graph_overrides`, так что оно переживает следующий
  `publish graph`; `rel add` — нет (загрузчик не трогает добавленные
  руками связи). Проверки здоровья графа (`pauk/admin/checks.py`,
  `graph_stats.py`, `health.py`) живут в админ-панели, не на сайте;
  `tests/unit/test_checks.py` сверяет лейблы/связи в их запросах со схемой.
- **Блокировки и фоновые задачи — `pauk/jobs/`.** Админ-панель не гоняет
  долгую работу в HTTP-запросе: пишет документ задачи в Mongo, отдельный
  процесс `pauk admin worker` её исполняет теми же функциями, что и CLI.
  Взаимоисключение — Mongo-коллекция `job_locks` через `held(...)`, и
  берут её сами функции пайплайна, а не воркер: `publish graph` и
  `dedup graph` держат общий лок графа, `enrich`/`run` — лок
  `group:<group>`. Занятый лок в CLI — это `Busy` → `SystemExit` с
  сообщением, а не трейсбек: ждать, а не чинить.

### Подпакеты

| Пакет | Что делает | Подробнее |
|---|---|---|
| `pauk/models/` | pydantic-схема prepared-слоя | [models.md](docs/architecture/models.md) |
| `pauk/sources/` | HTTP-клиенты внешних API и LLM | [sources.md](docs/architecture/sources.md) |
| `pauk/storage/` | raw/prepared в MongoDB | [storage.md](docs/architecture/storage.md) |
| `pauk/pipeline/` | collect → normalize → enrich → dedup | [pipeline/overview.md](docs/architecture/pipeline/overview.md) |
| `pauk/graph/` | prepared → Neo4j, дедуп/анмерж/prune на уровне графа | [neo4j-graph.md](docs/architecture/neo4j-graph.md) |
| `pauk/cache/` | снепшот графа на диск, дальше его читает `pauk/gui/` | [cache.md](docs/architecture/cache.md) |
| `pauk/gui/` | снепшот → раскладка → JSON + сайт на TS | [gui.md](docs/architecture/gui.md) |
| `pauk/admin/` | веб-панель (FastAPI + Jinja): правка графа, ревью, здоровье БД | [admin-panel.md](docs/admin-panel.md) |
| `pauk/jobs/` | очередь задач панели, воркер, Mongo-локи | [admin-panel.md](docs/admin-panel.md) |
| `pauk/cli.py` | команды `pauk ...` | [cli.md](docs/architecture/cli.md) |
| — | деплой на лабораторный сервер (`scripts/deploy.sh`, systemd-юниты) | [deploy.md](docs/architecture/deploy.md) |

`pauk/settings.py` — один датакласс `Settings` на всё: пути данных, ключи
API, параметры Neo4j/Mongo. Читает `.env` (см. `.env.example`).

### Схема Neo4j

Узлы: `Department`, `Organization`, `Person {is_itmo}`, `Publication`,
`Repository`, `GitHubProfile`, `LinkCandidate`. Основные связи:
`BELONGS_TO`, `AUTHORED`, `CONTRIBUTED_TO`, `PART_OF`, `PRODUCED_BY`,
`MENTIONS_LINK`, `DEVELOPED_BY`, `IMPLEMENTS`, `OWNED_BY`. Сотрудник ИТМО —
это свойство `p.is_itmo`, а не лейбл. Полный список свойств и
Mermaid-диаграмма: [`docs/architecture/neo4j-graph.md`](docs/architecture/neo4j-graph.md),
[`docs/diagrams/neo4j-schema.md`](docs/diagrams/neo4j-schema.md).

### Скрипты

`scripts/` — разовые/поддерживающие инструменты вне обычного пайплайна
(деплой, раздача статики на сервере, починка дедупа, импорт/проверка
курируемых репозиториев, сборка каталога департаментов, снепшот/репетиция
Mongo). Индекс с пометкой, какие из них пишут в базу:
[`docs/architecture/scripts.md`](docs/architecture/scripts.md).
