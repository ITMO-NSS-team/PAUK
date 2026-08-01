# PAUK

PAUK собирает публикации, обогащает их данными внешних источников и загружает
результат в Neo4j.

## Данные

```text
data/static/                 # версионируемые справочники, включая departments_catalog.json
data/raw/<group>/            # полные неизменяемые ответы API
data/prepared/<group>/       # JSONL сущностей для Neo4j
```

В prepared-группе создаются `publications.jsonl`, `persons.jsonl`,
`departments.jsonl`, `repositories.jsonl`, `github_profiles.jsonl` и
`repo_links.jsonl`.

## Запуск

```bash
# Одна публикация OpenAlex
pauk run --work W2741809807

# Публикации за период
pauk run --from 2025-01-01 --to 2025-03-31

# Произвольный список OpenAlex ID
pauk run --works-file selected_works.txt --name selected-july

# Отдельные шаги
pauk collect --work W2741809807
pauk normalize --group 2026-07-31__W2741809807
pauk enrich code_links --input data/prepared/2026-07-31__W2741809807/publications.jsonl
pauk publish graph --group 2026-07-31__W2741809807
```

Передача entity-файла в `enrich --input` ограничивает запуск строками именно
этого файла; передача директории группы запускает этап для всей группы.
Повторный `collect` не добавляет уже сохранённые OpenAlex works, а повторный
`normalize` сохраняет данные enrichment и производные entity-файлы.
Флаг `enrich --force` переобрабатывает и строки со статусом `completed`
(например, после исправления этапа).

Ссылки на код сейчас извлекаются только из абстрактов OpenAlex; поля
`context` и `page_number` у `MENTIONS_LINK` рассчитаны на разбор PDF,
который пока не реализован.

Каждая строка prepared JSONL содержит `_processing` со статусом этапа:
`not_started`, `completed`, `completed_empty`, `not_applicable` или `failed`.
Это поле не загружается в граф.

## Схема графовой БД

`pauk publish graph --group <group>` загружает prepared JSONL в Neo4j через
`MERGE`. Для всех типов узлов уникален `id`; у `GitHubProfile` также уникален
`login`.

`id` персоны — голый OpenAlex ID автора (один человек — один узел). Метка
`Itmo` присваивается, если хотя бы в одной работе встретилась аффилиация
ИТМО, и не понижается обратно до `External` данными других групп.
Репозитории, чей этап `repositories` завершился со статусом `failed`
(например, 404), в граф не загружаются до успешного ретрая — их ссылки
остаются узлами `LinkCandidate`.

| Узел | Метка Neo4j | Основные свойства |
|---|---|---|
| Подразделение | `Department` | `id`, `name_en`, `name_ru`, `name_variants` |
| Сотрудник ИТМО | `Person:Itmo` | `id`, `openalex_id`, `orcid`, ФИО, контакты, профили |
| Внешний автор | `Person:External` | `id`, `openalex_id`, `orcid`, `name_en`, `name_variants`, `email` |
| Публикация | `Publication` | `id`, `title`, `doi`, дата, журнал, код, funding, OpenAlex/PDF URL, abstract |
| Репозиторий | `Repository` | `id`, `name`, `url`, описание, звёзды, лицензия, даты |
| GitHub-профиль | `GitHubProfile` | `id`, `login`, `name`, URL, описание, location, type |
| Кандидат ссылки | `LinkCandidate` | `id` (URL), `url`, `host` |

Связи:

```text
(:Person:Itmo)     -[:BELONGS_TO]->  (:Department)
(:Person:Itmo)     -[:AUTHORED]->    (:Publication)
(:Person:External) -[:AUTHORED]->    (:Publication)
(:Person:Itmo)     -[:CONTRIBUTED_TO]-> (:Repository)

(:Publication) -[:PRODUCED_BY]-> (:Department)
(:Publication) -[:MENTIONS_LINK]-> (:Repository | :LinkCandidate)

(:Repository) -[:DEVELOPED_BY]-> (:Department)
(:Repository) -[:IMPLEMENTS]->   (:Publication)
(:Repository) -[:OWNED_BY]->     (:GitHubProfile)
```

У `AUTHORED` сохраняются `position`, `affiliation`, `is_corresponding`; у
`CONTRIBUTED_TO` — `role`; у `MENTIONS_LINK` — контекст, номер страницы и
результат проверки ссылки. Служебное `_processing` и поля, не перечисленные в
`pauk/graph/extract.py`, в Neo4j не попадают.

## GUI

Визуализация расположена в `pauk/gui`. После загрузки данных в Neo4j можно
сгенерировать статические данные и запустить веб-интерфейс:

```bash
python -m pauk.gui.generate_data
python -m pauk.gui.serve
```
