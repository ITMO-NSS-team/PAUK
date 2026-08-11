# Обзор архитектуры

**Что здесь:** сквозной поток данных через весь проект и таблица
подпакетов `pauk/` со ссылками на их отдельные заметки. Читать первым,
до любой другой заметки в `architecture/`.

**Какие файлы задействует:** ничего конкретного — обзор поверх всего
`pauk/`; за деталями каждого подпакета — по ссылкам ниже.

PAUK собирает публикации ИТМО из OpenAlex, обогащает их данными внешних
источников (Crossref, ORCID, OpenReview, GitHub), находит в них ссылки на
код и загружает результат в Neo4j. `pauk/gui/` читает граф и рисует
интерактивную карту.

Один Python-пакет `pauk/`. Старый `data_enrichment/`/`scripts/` (SQL-мост,
самостоятельный конвейер на pydantic-объектах) удалён целиком при переезде
на текущую архитектуру (PR #59, коммит `2a153c1`) — если где-то видите
упоминание этих путей, это история, не текущий код.

## Поток данных

```
OpenAlex API
  │  pauk collect
data/raw/<group>/openalex_works.jsonl        RawStore, append-only, fsync на строку
  │  pauk normalize
data/prepared/<group>/*.jsonl                PreparedStore, 6 плоских файлов
  │  pauk enrich [stage]                     pdf → persons → departments → code_links → repositories → dedup
data/prepared/<group>/*.jsonl                те же файлы, обогащённые
  │  pauk publish graph
Neo4j                                        накопление между прогонами, MERGE
  │  pauk dedup graph (по требованию)
Neo4j                                        схлопывает дубли across групп
  │  pauk cache export
data/cache/graph_snapshot.json               снепшот на диске
  │  pauk.gui.generate_data
pauk/gui/web/graph-data.js, graph-search.js  статика для карты
```

`<group>` — папка на один прогон: `<дата>__<work_id>` для одной публикации,
`<дата>__from_<start>__to_<end>` для периода, либо явное имя через `--name`
(`pauk/storage/naming.py::group_name`). `data/prepared/<group>/` — шесть
файлов, не единый агрегат: `publications.jsonl`, `persons.jsonl`,
`departments.jsonl`, `repositories.jsonl`, `github_profiles.jsonl`,
`repo_links.jsonl` (`PreparedStore.FILES`).

**Neo4j — единственное место, где данные разных прогонов накапливаются.**
Каждый `pauk publish graph --group <group>` льёт свою группу через
`MERGE ... ON CREATE / ON MATCH`. Чтение только последней группы JSONL
теряет всё, что было собрано раньше — поэтому кеш и визуализация читают
граф, а не файлы с диска напрямую.

## Подпакеты

| Пакет | Что делает | Подробнее |
|---|---|---|
| `pauk/models/` | pydantic-схема prepared JSONL | [models.md](models.md) |
| `pauk/sources/` | HTTP-клиенты внешних API | [sources.md](sources.md) |
| `pauk/storage/` | чтение/запись JSONL, атомарность, блокировки | [storage.md](storage.md) |
| `pauk/pipeline/` | collect → normalize → enrich → dedup | [pipeline/overview.md](pipeline/overview.md) |
| `pauk/graph/` | JSONL/граф → Neo4j, дедуп на уровне графа | [neo4j-graph.md](neo4j-graph.md) |
| `pauk/gui/` | снепшот → раскладка → статический сайт | [gui.md](gui.md) |
| `pauk/cache/` | снепшот графа на диск, дальше gui его читает | [cache.md](cache.md) |
| `pauk/cli.py` | команды `pauk ...` | [cli.md](cli.md) |
| — | деплой на лабораторный сервер | [deploy.md](deploy.md) |

`pauk/settings.py` — один `Settings`-датакласс на всё: пути данных, ключи
API, параметры Neo4j. Читает `.env` (см. `.env.example` в корне репо).

## Резюмируемость

Каждая pydantic-модель prepared-слоя несёт `processing: dict[str,
ProcessingState]` (алиас `_processing` в JSON) — статус каждого
enrichment-этапа на этой строке: `not_started`, `completed`,
`completed_empty`, `not_applicable`, `failed`. `EnrichmentStage.needs_attempt()`
(`pauk/pipeline/stages/base.py`) решает, трогать ли строку заново:
`FAILED`/`NOT_STARTED` — да, всё остальное — нет, если не передан
`--force`. Резюмируется между прогонами `pauk enrich` — `write_models()`
сохраняет результат в конце каждого `run()`.

## Публикация в граф

`pauk publish graph --group <group>` (или `--input <файл/папка>` для
точечного прогона на подмножестве строк) грузит сначала все узлы, потом
все связи — если связь ссылается на не загруженный узел, она не создаётся
(в лог идёт warning с точным числом, заглушки не заводятся). Подробности
— [neo4j-graph.md](neo4j-graph.md).

## Дедупликация — два уровня

- **Внутри одной группы** (`pipeline/stages/dedup.py`, этап `dedup`,
  всегда часть `pauk enrich`/`pauk run`): чистая, локальная, без сети —
  схлопывает персон/публикации/репозитории, у которых нашлось совпадение
  внутри JSONL этого прогона.
- **По всему графу** (`pauk/graph/dedup.py`, `pauk dedup graph`, отдельная
  команда, не часть обычного прогона): те же правила слияния, но читает
  весь Neo4j — ловит дубли, чьи записи попали в граф из **разных** групп и
  никогда не встречались бок о бок на диске.

Обе используют одни и те же функции принятия решений (`plan_person_merges`
и парная логика публикаций/репозиториев) — расхождение только в источнике
строк (JSONL vs Cypher) и в месте, куда льётся результат. Подробнее —
[pipeline/dedup.md](pipeline/dedup.md).
