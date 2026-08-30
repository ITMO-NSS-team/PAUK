# Полный прогон PAUK (end-to-end)

Сквозной цикл: сбор -> обогащение -> граф -> web. Первый прогон делают на копии
БД; прод трогают после проверки на копии (раздел 7).

Команды даны против `main`. Порядок и имена стадий - `ALL_STAGES` в
`pauk/pipeline/stages/__init__.py`.

Обозначения: `<server>` - хост с боевой (прод) БД, где Mongo крутится в
контейнере `pauk-mongo`; `<MONGO_URI>` - строка подключения к прод-Mongo из
серверного `.env`; «прод» - боевые Mongo и Neo4j на `<server>`.

## Требуется

- Установлены Docker и `uv`.
- Клон репозитория с зависимостями: `git clone <repo> pauk && cd pauk && uv sync`.
- SSH-доступ к `<server>`.
- Файл `data/static/russian_names.csv` (каталог сотрудников, персональные данные,
  в репозитории не хранится) - нужен стадии `author_names` (без него полный
  enrich падает). Положить его в `data/static/` или указать путь через
  `PAUK_RUSSIAN_NAMES_FILE`.

## 0. Pre-flight

Проверить место на диске и занятые порты:

```bash
df -h
docker ps
```

Порты `27018`, `7688`, `8501` должны быть свободны. Ещё нужна свободная RAM под
Mongo и Neo4j (на Linux - `free -g`). Наличие ключей проверяется после настройки
`.env` (раздел 1).

Для полного круга нужны `OPENALEX_API_KEY`, `GITHUB_TOKEN`,
`OPENREVIEW_USERNAME`/`OPENREVIEW_PASSWORD`, `OPENROUTER_API_KEY`
(+ `OPENROUTER_PROXY_URL`, если OpenRouter недоступен из сети напрямую).

## 1. Копия БД

### Снять дамп

`<MONGO_URI>` - строка подключения из серверного `.env`. На Windows запускать из
Git Bash.

```bash
ssh <server> "docker exec pauk-mongo mongodump --uri='<MONGO_URI>' --archive --gzip" > pauk_dump.archive.gz
```

### Поднять локальные Mongo и Neo4j

```bash
docker run -d --name pauk-mongo-copy -p 27018:27017 mongo:4.4 --wiredTigerCacheSizeGB 0.5
until docker exec pauk-mongo-copy mongo --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do sleep 1; done
docker exec -i pauk-mongo-copy mongorestore --archive --gzip < pauk_dump.archive.gz

docker run -d --name pauk-neo4j-copy -p 7688:7687 \
  -e NEO4J_AUTH=neo4j/testtest \
  -e NEO4J_server_memory_heap_max__size=1G \
  -e NEO4J_server_memory_pagecache_size=512m \
  neo4j:2026.05.0
```

### Направить окружение на копию

В `.env` репозитория (из «Требуется») прописать локальные адреса БД и ключи:

```
MONGO_URI=mongodb://localhost:27018
MONGO_DB=pauk
NEO4J_URI=bolt://localhost:7688
NEO4J_USER=neo4j
NEO4J_PASSWORD=testtest
OPENALEX_API_KEY=<ключ>
GITHUB_TOKEN=<ключ>
OPENREVIEW_USERNAME=<логин>
OPENREVIEW_PASSWORD=<пароль>
OPENROUTER_API_KEY=<ключ>
OPENROUTER_PROXY_URL=<url, если OpenRouter недоступен из сети напрямую>
```

Проверить, что окружение смотрит на копию и ключи на месте (адреса - локальные,
ключи - `True`):

```bash
uv run python -c "from pauk.settings import settings as s; print('db:', s.mongo_uri, s.neo4j_uri); print('keys:', {k:bool(getattr(s,k)) for k in ['openalex_api_key','github_token','openreview_username','openreview_password','openrouter_api_key']})"
```

## 2. Сбор и обогащение

`testrun` в примерах - имя группы, которое вы выбираете сами и используете одно и
то же во всех шагах (`--name` задаёт его, `--group` на него ссылается). Без
`--name` имя генерируется автоматически с датой; для многодневного прогона
фиксировать `--name`. Диапазон `--from`/`--to` - под ваш прогон.

Одной командой (collect + normalize + enrich; publish отдельно):

```bash
uv run pauk run --from 2025-01-01 --to 2025-03-31 --name testrun
```

Пофазно:

```bash
uv run pauk collect --from 2025-01-01 --to 2025-03-31 --name testrun
uv run pauk normalize --group testrun
uv run pauk enrich --group testrun
```

Стадии (порядок исполнения):
`pdf -> persons -> departments -> code_links -> link_relevance -> emails ->
repositories -> dedup -> github_match -> author_names` (+ `social_graph`,
опционально). Одна стадия: `uv run pauk enrich <stage> --group testrun`.

Стадия `dedup` сворачивает дубли внутри одной группы. Дедуп всего графа -
отдельная команда в разделе 3.

## 3. Граф

```bash
uv run pauk publish graph --group testrun     # по группам
uv run pauk dedup graph                         # по всему графу
```

`dedup graph` на большом графе идёт десятки минут; фаза планирования пишет в лог в
конце. Сведённые слияния и отложенные пары - в `data/cache/dedup_candidates_graph.jsonl`.

## 4. Обновление web

```bash
uv run pauk cache export                        # -> data/cache/graph_snapshot.json
uv run python -m pauk.gui.generate_data         # -> pauk/gui/data/private/graph-data.js, graph-search.js
uv run python -m pauk.gui.generate_stats        # -> pauk/gui/data/private/graph-stats.js
uv run python -m pauk.gui.serve                 # порт 8501
```

После правок графа пересобирать web этой же цепочкой; открыть `http://localhost:8501`.

## 5. Проверка

- Health-таб на странице (`http://localhost:8501`) -> «Пересчитать» (проверки `pauk/gui/checks.py`).
- Счётчики графа:

  ```bash
  uv run python -c "from neo4j import GraphDatabase; from pauk.settings import settings as s; d=GraphDatabase.driver(s.neo4j_uri,auth=(s.neo4j_user,s.neo4j_password)); ses=d.session(); print({l:ses.run(f'MATCH (n:{l}) RETURN count(n) AS c').single()['c'] for l in ['Publication','Repository','Person','Department']}); d.close()"
  ```

## 6. Догнать частичный прогон

Что прошло не всё, видно по:

- группам в Mongo против опубликованного в граф;
- счётчикам графа против Mongo (меньше в графе - группа недопубликована);
- распределению статусов по стадиям (`_processing` - поле в каждом документе со
  статусом каждой стадии):

  ```bash
  uv run python -c "from pymongo import MongoClient; from pauk.settings import settings as s; import collections,pprint; db=MongoClient(s.mongo_uri)[s.mongo_db]; c=collections.Counter(); [c.update({(k,(v or {}).get('status')):1 for k,v in (p.get('_processing') or {}).items()}) for p in db.persons.find({},{'_processing':1})]; pprint.pprint(dict(c))"
  ```

  Статусы: `completed` / `completed_empty` / `failed` / `not_started` /
  `not_applicable`.

Точечный догон:

| Что | Команда |
|---|---|
| стадия не доехала / есть `failed` | `uv run pauk enrich <stage> --group <группа>` (берёт не-completed) |
| переделать и `completed` | добавить `--force` |
| только конкретные id | `--input ids.txt --entity <entity>` |
| группа недопубликована | `uv run pauk publish graph --group <группа>` |
| дубли после доливки | `uv run pauk dedup graph` |
| граф изменился, web устарел | `cache export -> generate_data -> generate_stats` |

`--entity` принимает ключи `PreparedStore.COLLECTIONS`: publications, persons,
departments, organizations, repositories, github_profiles, repo_links.

## 7. Промоут в прод

После проверки на копии повторить разделы 2-5 с прод-окружением: в `.env`
репозитория вернуть прод-адреса БД (из серверного `.env`) вместо локальных.

Убрать копию по завершении:

```bash
docker rm -f pauk-mongo-copy pauk-neo4j-copy
```

## Чек-лист

```
[ ] 0. pre-flight: диск / RAM / порты / ключи
[ ] 1. дамп -> restore в локальный Mongo -> локальный Neo4j; env на копию, проверить
[ ] 2. pauk run --from … --to … --name testrun
[ ] 3. pauk publish graph --group testrun ; pauk dedup graph
[ ] 4. cache export ; generate_data ; generate_stats ; serve
[ ] 5. health-таб ; счётчики графа
[ ] 6. догон точечно при недоборе
[ ] 7. проверка на копии пройдена -> повторить на проде
```
