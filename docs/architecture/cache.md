# `pauk/cache/` — снепшот графа

**Что здесь:** как граф Neo4j снимается в файл-снепшот на диске, который
дальше читает `pauk/gui/`.

**Какие файлы задействует:** `pauk/cache/export.py`, `graph_snapshot.py`,
`inspect.py`, `__init__.py`.

Единственное место в `pauk/gui`-цепочке, которое реально ходит в Neo4j.
Всё остальное (`pauk/gui/graph_builder/builder.py`)
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
то, что `pauk/gui/graph_builder/builder.py::GraphDataBuilder` ожидает на входе.
Департаменты авторов и владельцы репозиториев — не плоские колонки в
графовой модели, а связи (`BELONGS_TO`, `OWNED_BY`), поэтому здесь они
отдельными запросами через `OPTIONAL MATCH`.

## `graph_snapshot.py`

`write_snapshot`/`read_snapshot` — запись (атомарно) и чтение плоского
словаря `load_db()` как JSON; `read_snapshot` кидает `ValueError`, если
верхний уровень файла не объект. Снепшот пишется в
`data/cache/graph_snapshot_<дд-мм-гггг>.json` (`dated_snapshot_path`), и
`latest_snapshot` находит самый свежий по дате в имени — его по умолчанию
берут `pauk cache inspect` и `pauk gui build`, а `gui build` пишет путь
выбранного снепшота в лог. Отдельной проверки "снепшот протух" по TTL нет:
дата видна в имени файла.
