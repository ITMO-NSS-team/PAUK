# `pauk/graph/` — коннектор и реальная схема графа

**Что здесь:** схема графа Neo4j, как её строит код, и как устроен
коннектор (prepared-строки из MongoDB → узлы/связи → загрузка → дедуп на
уровне графа).

**Какие файлы задействует:** `pauk/graph/extract.py`, `jsonl_loader.py`,
`client.py`, `audit.py`, `schema.py`, `csv_loader.py`, `dedup.py`,
`load.py`, `pauk/urls.py`.

Диаграмма той же схемы как Mermaid — [`diagrams/neo4j-schema.md`](../diagrams/neo4j-schema.md).
Контекст, почему словарь связей именно такой, а не по предложенной
Камилем схеме — [`journal/2026-08-01-kamil-schema-stub-fields.md`](../journal/2026-08-01-kamil-schema-stub-fields.md).

## Узлы и связи

| Узел | Уникальный ключ | Метки |
|---|---|---|
| Person | `id` (голый OpenAlex author ID) | `Person:Itmo` или `Person:External` |
| Department | `id` (uid-слаг из `name_en`) | `Department` |
| Organization | `id` и `name_en` (оба уникальны) | `Organization` |
| Publication | `id` (голый OpenAlex work ID) | `Publication` |
| Repository | `id` и `url` (оба уникальны) | `Repository` |
| GitHubProfile | `id` и `login` (оба уникальны) | `GitHubProfile` |
| LinkCandidate | `id` (сам URL) | `LinkCandidate` |

```text
(:Person:Itmo)     -[:BELONGS_TO]->     (:Department)
(:Person:Itmo)     -[:AUTHORED]->       (:Publication)
(:Person:External) -[:AUTHORED]->       (:Publication)
(:Person:Itmo)     -[:CONTRIBUTED_TO]-> (:Repository)

(:Department)  -[:PART_OF]->       (:Department | :Organization)

(:Publication) -[:PRODUCED_BY]->   (:Department)
(:Publication) -[:MENTIONS_LINK]-> (:Repository | :LinkCandidate)

(:Repository) -[:DEVELOPED_BY]-> (:Department)
(:Repository) -[:IMPLEMENTS]->   (:Publication)
(:Repository) -[:OWNED_BY]->     (:GitHubProfile)
```

`AUTHORED` несёт `position`/`affiliation`/`affiliation_source`/
`is_corresponding`; `CONTRIBUTED_TO` — `role`; `MENTIONS_LINK` — `context`
(список), `page_number` (список, `0` = абстракт — Neo4j не хранит `null`
внутри массива-свойства, поэтому сентинел не `None`, см.
[pipeline/code-links.md](pipeline/code-links.md)), `is_relevant`,
`llm_confidence`, `llm_reason`.

Person смёржен на базовую метку `:Person` (не на полную пару
`Person:Itmo`/`Person:External`) — один и тот же автор может быть ИТМО в
одной группе и внешним в другой; `:Itmo` — «липкая» метка, внешняя строка
никогда не понижает уже проставленный `:Itmo` (`client.py::upsert_person_nodes_batch`).

Иерархия подразделений рекурсивна: каждый `Department` `PART_OF` ровно одного
родителя — другого `Department` (`parent_id`) или корневого `Organization`
(`organization_id`), — так `кафедра → факультет → мегафакультет → организация`
собирается цепочкой рёбер одного типа. Пополевое описание всех узлов и связей —
в [`diagrams/neo4j-schema-desc.md`](../diagrams/neo4j-schema-desc.md).

## `extract.py` — декларативный реестр

`NODE_REGISTRY: dict[str, NodeSpec]` — по одному рецепту на тип строки
prepared JSONL. `NodeSpec` несёт белый список простых свойств
(`prop_fields` — всё, чего нет в списке, в узел не попадает, это и есть
защита от мусорных полей вроде отладочных значений) и список `RelSpec` —
какие поля строки на самом деле спрятанные связи. `RelSpec` умеет:

- **скалярное поле** (`scalar=True`) — одно значение, не список
  (`Repository.owner_login` → `OWNED_BY`);
- **список голых id** (`tgt_id_field=None`) — например `department_ids`;
- **список объектов с полями-свойствами связи** — `authored` →
  `AUTHORED`, с `prop_fields=("position", "affiliation", ...)`;
- **дискриминированное поле** (`guard`) — `mentions_links`/`repo_links`
  ведут либо на `Repository`, либо на `LinkCandidate`, различаются по
  `target_kind` — это два разных `RelSpec` на одно поле данных, каждый со
  своим `guard`.

`extract_node`/`extract_relationships` — чистые функции `dict -> dict`, ни
одна не ходит в сеть, поэтому тестируются без живого Neo4j
(`tests/unit/test_graph_extract.py`).

Вложенные map/list-of-map (`funding`, `versions`, `affiliations`,
`counts_by_year`) Neo4j как свойство узла не хранит — `extract_node`
сериализует их в JSON-текст (`JSON_TEXT_FIELDS`).

## `jsonl_loader.py` — порядок загрузки

Жёсткое правило: сначала загружаются **все** узлы, только потом **все**
связи. Если связь ссылается на узел, которого ещё нет в базе — она просто
не создаётся (в лог идёт warning с точным числом непроставленных связей) —
заглушка-узел не заводится никогда, это осознанное решение.

`repo_links.jsonl` — не узел, обрабатывается отдельно
(`extract_repo_links()`): для каждой ссылки сверяет URL с уже известными
`Repository.url` (через `normalize_repo_url` — регистронезависимо, без
`www.`/трейлинг-слэша/`.git`); совпало — `MENTIONS_LINK` на `Repository`;
не совпало — заводится `LinkCandidate` на лету и связь на него. Это
**реальный путь** загрузки ссылок в граф — `Publication.mentions_links`
не читается вообще, несмотря на то, что рецепт для него в `NODE_REGISTRY`
формально существует.

Строка `repositories.jsonl`, у которой стейдж `repositories` завершился
статусом `failed` — пропускается: `name`/`url`-заглушка от неуспешного
запроса к GitHub API не грузится в граф до успешного ретрая, а ссылка на
неё остаётся `LinkCandidate`.

В конце каждой загрузки — `promote_link_candidates_batch`: если
предыдущий publish создал `LinkCandidate`, пока GitHub был недоступен, а
теперь репозиторий успешно зарезолвился — старые связи переносятся на
`Repository` с сохранением свойств, кандидат без других ссылок удаляется.
И `fetch_merged_id_map` на каждый лейбл: если этот конкретный publish
принёс id, который граф-дедуп уже когда-то схлопнул в другой узел —
перефолдить сразу, не дожидаясь следующего `pauk dedup graph`.

## `client.py` — как говорим с Neo4j

Батчевый `UNWIND ... MERGE`, чанки по `CHUNK_SIZE = 2000`.
`upsert_relationships_batch` возвращает число реально совпавших пар
источник/цель — Neo4j-счётчик `relationships_created` для этого не
годится: он остаётся `0`, когда `MERGE` находит уже существующую связь
(повторный прогон), и это не ошибка, а норма.

Конструктор `Neo4jClient.__init__` не создаёт констрейнты — это отдельный
явный шаг, `schema.create_constraints()`, до самого первого прогона
данных. Пустой пароль — сразу понятный `ValueError`, а не поздняя ошибка
аутентификации от самого драйвера при первом запросе.

`_fold_nodes_batch` — общий механизм схлопывания дублей (используется
`merge_person_nodes_batch`/`merge_publication_nodes_batch`/
`merge_repository_nodes_batch`, вызывается из `pauk/graph/dedup.py`):
переносит все исходящие/входящие связи с дубля на канонический узел
(существующая связь канонического узла побеждает, свойства дубля только
заполняют пробелы — `SET new += properties(old); SET new += keep`,
Cypher-идиома «не дать дублю выиграть»), поля самого узла — через
Python-логику в `_merge_duplicate_properties` (списки — union с
сохранением порядка, булевы — OR, JSON-списки — распаковка+union+запаковка
обратно), затем `DETACH DELETE` дубля. Свойства узла нельзя слить прямо в
Cypher тем же трюком, что и связи — это на мгновение выставило бы
`canonical.id` в id дубля, пока дубль ещё существует, и упало бы на
констрейнте уникальности.

## `audit.py` — журнал изменений

`AuditedNeo4jClient` — прозрачная обёртка вокруг `Neo4jClient`: перехватывает
только мутирующие методы (`upsert_*_batch`, `merge_*_batch`,
`promote_link_candidates_batch`), всё остальное (`fetch_*`, `close`, доступ к
`driver`) уходит в исходный клиент через `__getattr__`. На каждый перехваченный
вызов — снапшот затронутых узлов/связей **до**, сам вызов, снапшот **после**,
диф по полям, запись в `AuditSink`. Если исходный вызов бросает исключение —
запись в лог не попадает вообще: аудит никогда не утверждает, что изменение
случилось, если оно не случилось.

Актор (кто меняет) и источник (откуда) обёртка берёт не из аргумента, а из
`contextvars` — `actor_context("user:...", source="admin-ui")`. Так
`jsonl_loader.py` и любой будущий CRUD-код не должны прокидывать актёра через
каждую сигнатуру, достаточно одного `with` на весь вызывающий код.

Батчи от `diff_threshold` (по умолчанию 50) строк и больше пишут одну грубую
запись `bulk_write` (только счётчик) без подиффа — диффить каждый узел
двухтысячного ETL-чанка удвоило бы число запросов почти без пользы для
аудита. Батчи меньше порога получают полный `AuditEntry` на строку с
`diff: dict[поле, (было, стало)]`.

`created_at`/`updated_at` (`TECHNICAL_DIFF_FIELDS`) исключены из дифа во всех
трёх ветках — `created`, `updated` и `deleted` — иначе `created`/`deleted`
записи тащат в диф технические поля, не относящиеся к реальному изменению.

Единственный на данный момент `AuditSink` — `JSONLAuditSink`, append-only JSONL
(`{timestamp, actor, source, operation, entity_type, entity_id, change_kind,
diff}` на строку). Запись в MongoDB на ряду с `JSONLAuditSink` —
рассматривается, но еще не реализована.

Единственный незакрываемый именно JSONL-синком разрыв: аудит-запись пишется
*после* коммита транзакции Neo4j, отдельным шагом — падение в этом узком окне
оставит граф изменённым без аудит-записи. Закрыть до конца можно только
записью аудита в той же транзакции, что и сама запись данных (будущий
`Neo4jAuditSink`, пишущий `:AuditEvent`-узлы тем же `execute_write`).

## `csv_loader.py`

Параллельный путь для общего CSV-формата (`id/labels/properties`), держится
про запас. Сегодня ни один этап пайплайна такой CSV не производит — грузить
нечем, но код рабочий, не мёртвый по конструкции.

## `dedup.py` — дедуп по всему графу

Отдельный документ — [pipeline/dedup.md](pipeline/dedup.md), там же и
про дедуп внутри одной группы: правила слияния общие, различается только
источник строк (Cypher вместо MongoDB) и место записи результата.

## `load.py`

Точка входа `pauk publish graph` — `load_jsonl_group(config, mongo_db,
group)` читает prepared-коллекции группы из MongoDB
(`PreparedStore.read_rows`) и передаёт строки в
`jsonl_loader.load_prepared_rows` (общая функция, источник строк ей не
важен). Отдельно — `uv run python -m pauk.graph.load` напрямую, с
флагами `--format jsonl|csv`, `--dir`: самостоятельный инструмент для
внешней папки JSONL/CSV, не завязан на MongoDB, использует
`jsonl_loader.load_jsonl_dir`/`csv_loader.load_csv_dir`. Оба пути создают
констрейнты, затем грузят выбранным способом, закрывают соединение в
`finally`.

## `urls.py`

Единственная функция вне `graph/`, `storage/` и `pipeline/`, которую
использует и пайплайн, и граф-слой: `normalize_repo_url()` — ключ
сравнения URL репозитория (регистр, `www.`, трейлинг-слэш, `.git` —
всё это косметика, без нормализации один и тот же репозиторий расщепился
бы на `Repository` и `LinkCandidate`).
