# `pauk/cli.py` — команды

**Что здесь:** справочник команд `pauk ...` — что каждая делает и с какими
флагами.

**Какие файлы задействует:** `pauk/cli.py`.

Один argparse-парсер, без typer/click. Точка входа — `pauk` (см.
`pyproject.toml`, `[project.scripts]`) или `uv run python -m pauk.cli`.

```
pauk [--verbose] <command> ...
```

`--verbose` включает DEBUG для модулей `pauk` и безопасную трассировку HTTP-запросов
`urllib3.connectionpool`. В URL значения чувствительных query-параметров (`api_key`,
`token`, `password`, подписи URL) заменяются на `[REDACTED]`. Логгер Neo4j остаётся
на WARNING, чтобы параметры Cypher-запросов не попадали в вывод. Из остальных
диагностических сообщений также удаляются известные секреты.

## `run` / `collect`

```
pauk run     --work <id> | --works-file <файл> | --from <дата> --to <дата>  [--name <имя>]
pauk collect --work <id> | --works-file <файл> | --from <дата> --to <дата>  [--name <имя>]
```

`run` = `collect` → `normalize` → `enrich` (все стейджи) одним вызовом,
**но не включает `publish graph`** — загрузка в общую Neo4j остаётся
отдельным ручным шагом (`PipelineRunner.run()`). `--work`/`--works-file`/
`--from`+`--to` взаимоисключающие способы задать выборку работ; `--name`
переопределяет автоматическое имя группы.

`collect` дополнительно перед сбором новых работ чинит уже сохранённые
записи, чей список авторов был обрезан лимитом list-эндпоинта OpenAlex
(`Collector.refetch_truncated()` — вызывается автоматически внутри каждого
`collect`, доступен и отдельно для починки уже собранной группы без
повторного обхода всего периода).

## `normalize`

```
pauk normalize --group <group>
```

Разбирает raw-коллекцию MongoDB (`openalex_works`, отфильтровано по
`group`) в prepared-коллекции `publications`/`persons`. Повторный запуск
на той же группе безопасен — сохраняет уже собранные enrichment-данные и
`_processing` существующих строк, мержит их с обновлённым содержимым
сырых данных (`OpenAlexNormalizer.run()`). Сущности в MongoDB глобальные:
если тот же work id уже встречался в другой группе, нормализация видит
его текущее состояние по id (`PreparedStore.get_models`), а не только то,
что уже накопила именно эта группа — см. [storage.md](storage.md).

## `enrich`

```
pauk enrich [stage] --group <group> [--input <файл-с-id> --entity <сущность>] [--force]
```

`stage` — имя одного этапа (`pdf`, `persons`, `departments`, `code_links`,
`repositories`, `dedup`) или `all` (по умолчанию, все по порядку — порядок
задан `ALL_STAGES` в `pipeline/stages/__init__.py`). `--group` обязателен
всегда. `--input` (вместе с `--entity`) сужает прогон до конкретных id —
файл, по одному id на строке (тот же формат, что у `--works-file` для
`collect`); `--entity` — имя одной из шести prepared-сущностей
(`PreparedStore.COLLECTIONS`), к которой относятся эти id. `--force`
заставляет перепрогнать строки, чей стейдж уже
`COMPLETED`/`COMPLETED_EMPTY`/`NOT_APPLICABLE` — нужен, когда логику
стейджа поменяли и старые (уже отмеченные завершёнными) строки нужно
пересчитать заново.

## `publish graph`

```
pauk publish graph --group <group>
```

Грузит prepared-коллекции группы из MongoDB в Neo4j: создаёт констрейнты,
затем все узлы, затем все связи. См. [neo4j-graph.md](neo4j-graph.md).
Отдельный `python -m pauk.graph.load --dir <папка>` — самостоятельный
инструмент для загрузки внешнего CSV-экспорта, не завязан на MongoDB и
на пайплайн вообще.

## `dedup graph`

```
pauk dedup graph
```

Дедуп персон/публикаций/репозиториев **по всему графу**, не по одной
группе — единственный способ поймать дубли, чьи записи попали в Neo4j из
разных прогонов и никогда не лежали рядом в одном JSONL. Не часть `run`,
запускается вручную, когда накопилось несколько групп. См.
[pipeline/dedup.md](pipeline/dedup.md).

## `cache export`

```
pauk cache export [--output <путь>]
```

Снимает снепшот текущего состояния графа в
`data/cache/graph_snapshot.json` (или указанный путь) — готовит вход для
`pauk.gui.generate_data`. См. [cache.md](cache.md).

## `--input`: точечный выбор строк у `enrich`

`_selection_from_input(path, entity)` читает `path` построчно (один id на
строку, пустые строки пропускаются) и строит `PreparedSelection(entity,
ids)` — `EnrichmentStage.selected()` пропускает всё, что не входит в этот
набор. Не привязан к MongoDB и не требует, чтобы файл лежал где-то
конкретно — это просто список id, источник которого может быть каким
угодно (вручную составленный, выгруженный запросом и т.д.).
