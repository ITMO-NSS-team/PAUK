# `pauk/models/` — схема prepared-слоя

**Что здесь:** какие сущности и поля есть в prepared-слое и что каждое
поле значит.

**Какие файлы задействует:** `pauk/models/publication.py`, `person.py`,
`department.py`, `repository.py`, `relations.py`, `processing.py`,
`__init__.py`.

Pydantic-модели того, что лежит в `data/prepared/<group>/*.jsonl`. Ни один
файл здесь не ходит в сеть и не содержит бизнес-логики — только форма
данных плюс несколько небольших вычисляемых свойств у моделей-родственников
(`Authorship`, `Contribution`).

## `publication.py`

- **`Publication`** — id = OpenAlex work ID без префикса URL. `type` —
  тип работы по OpenAlex (`article`, `preprint`, `software`, `dataset`,
  ...) — используется, например, в `code_links.py` для детекта архивных
  Zenodo-депозитов ([pipeline/code-links.md](pipeline/code-links.md)).
  `fields` — топики OpenAlex верхнего уровня, используются дедупом персон
  как слабый сигнал совпадения по научной области. `full_text` — текст,
  извлечённый из PDF (может быть `None`, если PDF не нашли/не смогли
  распарсить — не путать с `abstract`, который приходит от OpenAlex
  напрямую). `mentions_links: list[MentionsLink]` — параллельное
  представление ссылок на код; действующий путь загрузки
  `MENTIONS_LINK`-связей в граф идёт через `repo_links.jsonl`
  (`RepoLink`/`CodeLink`), см. [neo4j-graph.md](neo4j-graph.md).
  `versions`/`merged_ids` — журнал слияний, см. ниже.
- **`PublicationVersion`** — одна запись OpenAlex, слитая в эту публикацию
  дедупом: препринт, версия записи, дубликат при переиндексации OpenAlex.
  Хранит title/doi/journal/дату/абстракт/авторов **этой конкретной
  записи** — так объединение остаётся без потерь: ни один источник данных
  не теряется при схлопывании дублей, всё остаётся доступным через
  `versions`, даже если выжившая публикация взяла другие значения полей.
- **`VersionAuthor`** — автор одной версии как её перечислял OpenAlex
  (`person_id`, `name`, `position`) — не то же самое, что текущий список
  авторов публикации (тот — через `Authorship`-связи на `Person`).
- **`Funding`** — грант: `funder`, `grant_id`.

## `person.py`

- **`Affiliation`** — место работы человека, как его называет один
  источник (`name`, `ror`, `years`, `source: "openalex" | "orcid"`). Нужен
  потому что self-deposit площадки (Zenodo, SSRN) часто не указывают
  аффилиацию соавтора у конкретной работы — тогда `PersonsStage` берёт
  аффилиацию по году из собственных записей автора (OpenAlex/ORCID) вместо
  того, что сказала сама работа.
- **`Person`** — id = голый OpenAlex author ID (один человек — один узел,
  без разделения на `itmo_*`/`external_*`, как было раньше). `is_itmo` —
  булево, не метка; хотя бы одна ИТМО-аффилиация где угодно навсегда
  делает человека ИТМО (см. `graph/client.py::upsert_person_nodes_batch` —
  свойство `is_itmo` «липкое»). Русские ФИО (`first_name_ru`/`second_name_ru`/
  `surname_ru`) — поля в модели, источник для них в пайплайне не
  подключён. `email` — адрес для карточки, один; `emails` — все известные
  адреса, по ним `github_match` опознаёт аккаунт (аккаунт подписан тем,
  которым коммитит, а не тем, который выбрали показывать).
  `github`/`google_scholar`/`openreview` заполняются тремя путями: ссылка,
  указанная автором в ORCID, профиль OpenReview и `github_match`.
- Блок под комментарием `# stub` (`scopus_id`, `researcher_id`, `h_index`,
  `wikipedia` и ещё около полутора десятков полей) — поля из
  предложенной Камилем схемы графа, ни один pipeline stage их не
  заполняет. Исключение — `other_names`, `homepage`, `linkedin` и
  `gitlab_username`: их заполняет `persons` из ORCID
  (см. [pipeline/persons.md](pipeline/persons.md)). Оставлены намеренно, чтобы модель/коннектор/визуализация уже
  знали форму данных, когда реальный источник появится — контекст решения
  в [`journal/2026-08-01-kamil-schema-stub-fields.md`](../journal/2026-08-01-kamil-schema-stub-fields.md).

## `department.py`

- **`Department`** — id, `name_en`, `name_ru`, `name_variants` (варианты
  написания, по которым `departments.py` матчит департамент в тексте
  аффилиации). Источник — `data/static/departments_catalog.json`
  (`pauk/storage/static.py::StaticStore`), id — `sha256` от `name_en`, так
  что id стабилен между прогонами без базы данных.

## `repository.py`

- **`Repository`** — id = `github_{owner}_{name}` в нижнем регистре после
  канонизации (см. [pipeline/dedup.md](pipeline/dedup.md)). `github_id` —
  числовой id GitHub, переживает переименования/передачу владения, ключ
  дедупа. `cited_urls` — все URL, под которыми на репозиторий когда-либо
  ссылались (до канонизации) — по ним граф-загрузчик резолвит старые
  ссылки на этот узел. `publication_ids` — только публикации, чьим
  авторским результатом признан репозиторий; обычные упоминания остаются в
  `RepoLink`. `merged_ids` — id репозиториев, схлопнутых в этот.
- **`GitHubProfile`** — аккаунт, найденный за репозиторием: владелец или
  автор коммитов (`login` уникален, отдельно от `id`). Кроме полей самого
  профиля (`name`, `company`, `location`, `description`) несёт то, против
  чего сопоставляют автора: `emails` и `commit_names` — из git-идентичности
  в коммитах, потому что страница профиля адрес обычно скрывает, и `repos`
  — репозитории, на которых аккаунт встретился. В граф из этого идёт
  только `company`: остальное — доказательства матчера и адреса живых
  людей, см. [neo4j-graph.md](neo4j-graph.md).
- **`LinkCandidate`** — ссылка на код, найденная в статье, но ещё не
  сопоставленная с известным `Repository` (`id` = сам URL).
- **`LinkOccurrence`** — одно вхождение ссылки: `context` (окружающий
  текст) + `page_number` (`None` = абстракт, PDF-страницы с 1). Введено в
  этой сессии вместе с PDF full-text — раньше `CodeLink` хранил одно
  значение `context`/`page_number` на ссылку, что не давало отразить
  ссылку, встретившуюся и в абстракте, и на нескольких страницах.
- **`CodeLink`** — url, host, `occurrences: list[LinkOccurrence]`,
  `is_relevant`/`llm_confidence`/`llm_reason` — заполняются
  `link_relevance.py` (кроме детерминированного Zenodo-архива), см.
  [pipeline/code-links.md](pipeline/code-links.md).
- **`RepoLink`** — обёртка `{publication_id, links: list[CodeLink]}`,
  ровно то, что лежит одной строкой в `repo_links.jsonl`.

## `relations.py`

Модели связей, встроенные в родительскую строку (не отдельные файлы
JSONL):

- **`Authorship`** — `publication_id`, `position`, `affiliation`,
  `affiliation_source` (`None`, если аффилиацию дала сама статья;
  `"openalex"`/`"orcid"`, если её подставил `PersonsStage` из-за пропуска
  в исходной записи), `is_corresponding`.
- **`Contribution`** — `repository_id`, `role`.
- **`MentionsLink`** — `target_kind: "repository" | "candidate"` плюс
  `context`/`page_number`/`is_relevant`/... — тот же дискриминированный
  формат, что и `CodeLink`. `graph/extract.py` знает рецепт для этого поля
  (`NODE_REGISTRY["publication"]`), но действующий путь загрузки идёт через
  `repo_links.jsonl` и `graph/jsonl_loader.py::extract_repo_links()` —
  см. [neo4j-graph.md](neo4j-graph.md).

## `processing.py`

- **`ProcessingStatus`** — `StrEnum`: `not_started`, `completed`,
  `completed_empty`, `not_applicable`, `failed`. Общий на все этапы; его
  единственная реальная работа — управлять ретраем в
  `EnrichmentStage.needs_attempt()` (`NOT_STARTED`/`FAILED` → повторить,
  всё остальное → пропустить без `--force`). Не пытайтесь читать в него
  дополнительный смысл про «почему» — причина сбоя идёт текстом в
  `ProcessingState.error`, не отдельным статусом.
- **`ProcessingState`** — `status`, `request_key`, `phase`, `attempts`,
  `finished_at`, `error`, `result_count`. `request_key` связывает результат
  с конкретным входом внешнего API (ORCID, DOI, email); `phase` нужен
  многошаговому OpenReview-поиску.

## `__init__.py`

Реэкспортирует всё перечисленное выше одним плоским списком — остальной
код всегда пишет `from pauk.models import X`, не лезет во внутренние
модули напрямую.
