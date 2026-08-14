# PAUK

PAUK собирает публикации, обогащает их данными внешних источников (промежуточное и загружает результат в Neo4j.

## Данные

```text
data/static/                 # версионируемые справочники, включая departments_catalog.json
MongoDB: raw                 # полные неизменяемые ответы API, по группам
MongoDB: publications/persons/departments/repositories/github_profiles/repo_links
                              # prepared-сущности для Neo4j, глобальные — не по группе
```

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
pauk enrich code_links --group 2026-07-31__W2741809807 --input selected_ids.txt --entity publications
pauk publish graph --group 2026-07-31__W2741809807
```

`enrich --group` обязателен всегда; `--input <файл> --entity <сущность>`
дополнительно сужает запуск до id, перечисленных в файле (по одному на
строку). Повторный `collect` не добавляет уже сохранённые OpenAlex works, а
повторный `normalize` сохраняет данные enrichment — в том числе если тот же
work попал в другую, пересекающуюся группу: сущности в MongoDB глобальные,
не по группе. Флаг `enrich --force` переобрабатывает и строки со статусом
`completed` (например, после исправления этапа).

Ссылки на код извлекаются из абстракта и, если есть `pdf_url` (или он
находится по DOI через `PAUK_PDF_CRAWLER_URL`, см. `.env.example`), из PDF
постранично (`pauk enrich code_links`, кэш PDF — `data/pdf/`) —
и голые упоминания вида `github.com/org/repo`, и настоящие гиперссылки.
Текст PDF сохраняется в `Publication.full_text`.

`context`/`page_number` у `MENTIONS_LINK` — список вхождений на публикацию
(абстракт и страницы PDF). Перенос через дефис на границе строки
склеивается, без дефиса — нет. Если скачать/распарсить PDF не удалось,
этап помечается `failed` и ретраится при следующем прогоне, но результат
по абстракту сохраняется.

Каждый prepared-документ содержит `_processing` со статусом этапа:
`not_started`, `completed`, `completed_empty`, `not_applicable` или `failed`.
Это поле не загружается в граф.

## Схема графовой БД

`pauk publish graph --group <group>` загружает prepared-коллекции этой
группы из MongoDB в Neo4j через `MERGE`. Для всех типов узлов уникален
`id`; у `GitHubProfile` также уникален `login`.

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
python -m pauk.gui.generate_stats
python -m pauk.gui.serve
```
