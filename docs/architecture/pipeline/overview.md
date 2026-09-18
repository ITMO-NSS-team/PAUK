# `pauk/pipeline/` — оркестрация

**Что здесь:** как связаны сбор данных, нормализация и запуск
enrichment-этапов; резюмируемость.

**Какие файлы задействует:** `pauk/pipeline/collect.py`, `normalize.py`,
`enrich.py`, `runner.py`, `selectors.py`, `stages/base.py`,
`stages/__init__.py`.

Четыре шага, каждый — отдельная команда CLI (см. [../cli.md](../cli.md)):
`collect` → `normalize` → `enrich [stage]` → (`dedup` внутри `enrich` как
последний стейдж). `publish graph` и `dedup graph` — уже не `pipeline/`,
а `pauk/graph/`.

## `collect.py::Collector`

Тянет сырые работы с OpenAlex в MongoDB (коллекция `raw`, источник
`openalex_works`, см. [../storage.md](../storage.md)). Три режима выборки (`pauk/pipeline/selectors.py`): `WorkSelector` (один
id), `WorksFileSelector` (файл со списком id, по одному на строку),
`PeriodSelector` (диапазон дат, курсорная пагинация по ROR ИТМО —
`ITMO_ROR_ID = "04txgxn49"`).

**Обрезанные списки авторов.** OpenAlex list-эндпоинт отдаёт не больше
100 авторств на работу без явного маркера обрезки — `_authors_truncated()`
считает payload обрезанным либо по `is_authors_truncated`, либо по
точному совпадению длины списка с лимитом. Для периодического сбора это
чинится сразу: обрезанная запись перезапрашивается через single-work
эндпоинт, у которого список полный всегда. `refetch_truncated()` вызывается
автоматически в начале каждого `collect`, но доступна и отдельно —
дозаполняет уже сохранённую группу, у которой список авторов оказался
обрезан лимитом list-эндпоинта, без повторного обхода всего периода.

Повторный сбор идёт с дедупом по уже сохранённым id (`known_ids`) — второй
`collect` на ту же выборку не плодит дубликатов сырых записей.

## `normalize.py::OpenAlexNormalizer`

Разбирает `openalex_works` (raw, MongoDB) в `Publication`/`Person`. Не тривиальный
проход — здесь же живёт:

- **Очистка publisher-разметки** (`_clean_markup`) — химия/физика
  депонируют формулы MathML/HTML-тегами (`<mml:math>`, `<sub>`), OpenAlex
  отдаёт заголовок как есть. Формула схлопывается в текст без внутренних
  пробелов и остаётся приклеенной к тому, что она индексирует
  (`monolayer WSe2`, не `monolayer WS e 2`), остальная разметка просто
  теряет теги.
- **Фильтр организаций в позиции автора** (`ORG_AUTHOR_NAME`) — ACL
  Anthology депонирует венью автором, консорциумы — коллектив; OpenAlex
  даже заводит на них author-сущности. Регекс по ключевым словам
  (`association`, `committee`, `consortium`, ...) не даёт им стать
  `Person`.
- **Локальный id для неопознанного автора** (`_fallback_person_id`) —
  свежая запись может прийти с `author.id = null`, но с именем и часто
  ORCID. Вместо потери авторства — детерминированный id (ORCID, если
  есть, иначе хэш имени), который дедуп потом сможет схлопнуть в реального
  автора, когда OpenAlex его распознает.
- **Лимит внешних соавторов** (`EXTERNAL_AUTHORS_LIMIT = 500`) —
  консорциумные статьи несут сотни авторов; все ИТМО-авторы сохраняются
  всегда, внешние — с потолком, чтобы одна эпидемиологическая консорция
  не забила граф.
- **Повторная нормализация не теряет обогащение.** Уже собранные
  enrichment-данные, `_processing` и слитые дедупом id (`merged_ids`) на
  существующей строке сохраняются при повторном прогоне — новый заход по
  сырым данным только дополняет, не затирает. Работает не только внутри
  одной группы: сущности в MongoDB глобальные, так что тот же work id из
  другой, пересекающейся группы видит то же самое состояние
  (`OpenAlexNormalizer._seed`, см. [../storage.md](../storage.md)).

## `enrich.py::Enricher` + `pipeline/stages/base.py`

`Enricher.run(stage_name, selection, force)` — прогоняет один этап или
все (`ALL_STAGES`, порядок фиксирован в `pipeline/stages/__init__.py`:
`pdf → persons → departments → code_links → link_relevance → emails →
repositories → repo_people → dedup → github_match → author_names`).
Блокировки на группу больше нет — атомарность на уровне документа даёт
сама MongoDB (см. [../storage.md](../storage.md)).

Порядок не произвольный: `emails` читает полный текст, скачанный
`code_links`, и идёт до `github_match`, чтобы найденный адрес мог опознать
аккаунт; `github_match` нужны и аккаунты, собранные `repo_people`, и
авторства, уже схлопнутые `dedup`.

`repositories` и `repo_people` — две половины одной работы, разведённые
намеренно: первая берёт метаданные репозитория, вторая — людей за ним.
У каждой свой `processing`-статус, поэтому устареть и быть перезапущенной
они могут порознь (см. [repo-people.md](repo-people.md)).

`OPTIONAL_STAGES` — стадии вне общего прогона, запускаются только по
имени. Там сейчас одна, `social_graph`: она идёт вширь от уже
подтверждённых аккаунтов, поэтому имеет смысл лишь после того, как
`github_match` кого-то подтвердил, и стоит сотни запросов к API за прогон.

`EnrichmentStage` — общий базовый класс:

- `needs_attempt(state)` — `True`, если `state is None` или статус
  `NOT_STARTED`/`FAILED`; всё остальное пропускается без `--force`.
- `selected(entity, id)` — фильтр по `PreparedSelection`, когда прогон
  ограничен `--input`.
- `in_scope(entity, id)` — то же, но селекция по **другой** сущности не
  фильтрует: стейдж, доходящий до своих строк через несколько сущностей,
  сам решает, что значит для каждой из них прогон, ограниченный
  публикациями.

Каждый стейдж — отдельный файл, см. соседние заметки:
[pdf.md](pdf.md), [persons.md](persons.md), [departments.md](departments.md),
[code-links.md](code-links.md), [emails.md](emails.md),
[repositories.md](repositories.md), [repo-people.md](repo-people.md),
[dedup.md](dedup.md),
[github-match.md](github-match.md), [social-graph.md](social-graph.md).

## Резюмируемость

Между прогонами `pauk enrich` — `_processing` на каждой строке переживает
процесс, повторный запуск трогает только `NOT_STARTED`/`FAILED` строки.
Каждый стейдж копит изменения в памяти и вызывает `write_models()` один
раз в конце `run()`, после цикла по всем строкам.
