# `pauk/cli.py` — команды

**Что здесь:** справочник команд `pauk ...` — что каждая делает и с какими
флагами.

**Какие файлы задействует:** `pauk/cli.py`.

Один argparse-парсер, без typer/click. Точка входа — `pauk` (см.
`pyproject.toml`, `[project.scripts]`) или `uv run python -m pauk.cli`.

```
pauk [--verbose] <command> ...
```

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

Разбирает `data/raw/<group>/openalex_works.jsonl` в
`publications.jsonl`/`persons.jsonl`. Повторный запуск на той же группе
безопасен — сохраняет уже собранные enrichment-данные и `_processing`
существующих строк, мержит их с обновлённым содержимым сырых данных
(`OpenAlexNormalizer._run()`).

## `enrich`

```
pauk enrich [stage] (--group <group> | --input <файл-или-папка>) [--force]
```

`stage` — имя одного этапа (`pdf`, `persons`, `departments`, `code_links`,
`repositories`, `dedup`) или `all` (по умолчанию, все по порядку — порядок
задан `ALL_STAGES` в `pipeline/stages/__init__.py`). `--input` вместо
`--group` ограничивает прогон конкретным файлом или папкой группы —
строится `PreparedSelection` по id строк этого файла, остальной пайплайн
их пропускает. `--force` заставляет перепрогнать строки, чей стейдж уже
`COMPLETED`/`COMPLETED_EMPTY`/`NOT_APPLICABLE` — нужен, когда логику
стейджа поменяли и старые (уже отмеченные завершёнными) строки нужно
пересчитать заново.

## `publish graph`

```
pauk publish graph (--group <group> | --input <файл-или-папка>)
```

Грузит подготовленную группу (или файл/папку) в Neo4j: создаёт
констрейнты, затем все узлы, затем все связи. См.
[neo4j-graph.md](neo4j-graph.md).

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

## `--input` против `--group`: как выбирается `enrich`/`publish`

`_input_group_and_selection()` принимает либо путь к папке группы внутри
`data/prepared/`, либо путь к одному из шести JSONL-файлов этой группы.
Во втором случае строится `PreparedSelection` из id строк этого конкретного
файла (для `repo_links`/`departments`/`repositories`/`github_profiles` — по
`id` или `publication_id` из голых dict, без модели; для
`publications`/`persons` — через полноценные pydantic-модели). Путь обязан
лежать непосредственно внутри `settings.prepared_dir` — попытка указать
файл снаружи или на два уровня вложенности кидает `ValueError`.
