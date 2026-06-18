# PAUK - мониторинг публикаций

## Установка

```bash
uv sync
cp .env.example .env
# отредактировать .env
```

## Запуск пайплайна

Один скрипт по всем шагам:

```bash
./run_pipeline.sh
# или с логом:
./run_pipeline.sh 2>&1 | tee logs/pipeline.log
```

Даты и лимиты задаются переменными в начале `run_pipeline.sh`.

Шаги по отдельности (все идемпотентны - повторный запуск дорабатывает
только новое):

```bash
# 1. Схема единой БД.
uv run python scripts/init_db.py

# 2. Публикации ИТМО из OpenAlex + авторы (persons_itmo / persons_external).
uv run python scripts/populate_publications.py \
    --start-date 2024-05-01 --end-date 2026-06-15

# 3. GitHub-часть: abstract+PDF -> ссылки на код (repo_links) -> вердикт LLM.
uv run python scripts/find_code_links.py

# 4. LLM-разметка департаментов ИТМО по аффилиациям -> persons_itmo.department.
uv run python scripts/enrich_departments.py

# 5. Русские ФИО (транслитерация name_en) -> persons_itmo.*_ru.
uv run python scripts/enrich_persons_ru.py

# 6. Чистый слой repositories + github_departments (GitHub API, split user/org).
uv run python scripts/build_repositories.py

# 7. Чистка дублей + производные связи (has_code, *_departments).
uv run python scripts/finalize.py        # --dry-run чтобы только посмотреть dedup
```

## Схема БД

Базовые сущности и связи:

| Таблица                   | Назначение                                                        |
|---------------------------|------------------------------------------------------------------|
| `publications`            | Публикации (+ `has_code`, `code_url`, `pdf_url`, `abstract`)      |
| `persons_itmo`            | Сотрудники ИТМО (+ `department`, `github`)                        |
| `persons_external`        | Внешние соавторы                                                  |
| `publication_authors`     | Авторство (публикация ↔ человек)                                  |
| `departments`             | Департаменты ИТМО (`name_en` + `name_variants`)                   |
| `publication_departments` | Публикация ↔ департамент (через департаменты её ИТМО-авторов)     |
| `github_departments`      | **GitHub-организации** (лаборатории): login, name, описание       |
| `repositories`            | Чистый слой репозиториев + метаданные GitHub                     |
| `repository_persons`      | Репозиторий ↔ человек (`owner` / `contributor`)                  |
| `repository_departments`  | Репозиторий ↔ департамент ИТМО                                    |
| `repository_publications` | Репозиторий ↔ публикация                                          |
| `repo_links`              | Staging: все кандидатные ссылки + вердикт LLM (доказательная база)|

## Конфигурация

Путь БД можно переопределить переменной окружения `PAUK_DB_PATH`. Остальные ключи - в `.env`.