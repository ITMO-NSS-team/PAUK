# `pauk/cache/` — снепшот графа

**Что здесь:** как граф Neo4j снимается в файл-снепшот на диске, который
дальше читает `pauk/gui/`.

**Какие файлы задействует:** `pauk/cache/export.py`, `graph_snapshot.py`,
`freshness.py`, `__init__.py`.

Единственное место в `pauk/gui`-цепочке, которое реально ходит в Neo4j.
Всё остальное (`generate_data.py`, `serve.py` за исключением `/api/stats`)
читает результат этого шага с диска, не базу.

## `export.py`

`GraphSnapshotExporter.export(path=None)` — открывает драйвер, читает
восемь запросов (`load_db()`) в плоские структуры, пишет
`data/cache/graph_snapshot.json` (или путь по флагу `--output`). Пустой
пароль Neo4j — сразу `ValueError`, не поздняя ошибка от драйвера.

`_execute_retrying()` — общий retry-цикл на `ServiceUnavailable`/
`SessionExpired`/`TransientError`/`OSError`, до `CYPHER_RETRIES = 5`
попыток с нарастающей паузой (`min(60, 5 * attempt)`). Две тонкие обёртки
поверх него:

- `cypher()` — строки как позиционные тюплы, для стабильных по форме
  таблиц (`publications`, `repositories`, ...);
- `cypher_dict()` — строки как dict по именам колонок Cypher, используется
  только для `persons`, потому что это единственная таблица, чья форма
  ожидаемо растёт (новые поля) — добавление колонки не требует правки
  позиционной распаковки в вызывающем коде.

`load_db()` возвращает плоский словарь из восьми ключей:
`persons`/`publications`/`repositories`/`departments`/`authorship`/
`person_depts`/`pub_depts`/`repo_pubs`/`repo_persons`/`repo_depts` — ровно
то, что `generate_data.py::build_graph_data()` ожидает на входе.
Департаменты авторов и владельцы репозиториев — не плоские колонки в
графовой модели, а связи (`BELONGS_TO`, `OWNED_BY`), поэтому здесь они
отдельными запросами через `OPTIONAL MATCH`.

## `graph_snapshot.py`

`write_snapshot`/`read_snapshot` — конверт вокруг `load_db()`'s словаря:
`schema_version`, `generated_at`, `graph`. Версия 2: строки
`repositories` и `persons` — словари (`cypher_dict`), не позиционные
кортежи, потому что обе растут колонками, а распаковка кортежа по позиции
живёт в нескольких местах `generate_data.py`. `read_snapshot` кидает
`ValueError`, если версия схемы не совпадает — снепшот от старой версии
кода не будет молча скормлен в несовместимый `generate_data.py`.

## `freshness.py`

`is_fresh(path, max_age)` — сравнивает mtime файла снепшота с TTL,
заготовка под предупреждение о том, что снепшот пора пересобрать
(`pauk cache export`).
