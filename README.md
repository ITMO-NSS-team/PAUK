# PAUK

1. Вытягивает из [OpenAlex](https://openalex.org) все публикации сотрудников
   ИТМО за заданный период.
2. Скачивает open-access PDF каждой публикации.
3. Сканирует эти PDF на ссылки, ведущие в репозитории с кодом
   (GitHub, GitLab, Hugging Face, Zenodo и т.п.).
4. Хранит всё это в одном локальном SQLite-файле.

Конечная цель: по каждой статье ИТМО понять, выкладывали ли авторы код к
ней и где он лежит.

## Структура проекта

```
PAUK/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version        # Python 3.12
├── .gitignore
├── scripts/               # шаги пайплайна
│   ├── config.py          # пути, эндпоинты API, общие константы
│   ├── init_db.py
│   ├── populate_publications.py
│   ├── fetch_papers.py
│   └── extract_repo_links.py
└── data/                  # не отслеживается git
    ├── itmo_research_opensource.db    # SQLite-файл с базой
    └── pdfs/                          # скачанные PDF, по одному на публикацию
```

## Установка

Проект использует [uv](https://docs.astral.sh/uv/) для управления
окружением и зависимостями.

```bash
# Развернуть зависимости из pyproject.toml + uv.lock в .venv/
uv sync

# Проверить, что окружение собралось
uv run python -c "import fitz, requests; print('ok')"
```

### API-ключ OpenAlex

Скрипты `scripts/populate_publications.py` и `scripts/fetch_papers.py`
ходят в API OpenAlex.
```python
# scripts/config.py
OPENALEX_API_KEY = "REPLACE_ME"
```

## Запуск пайплайна

Четыре скрипта в `scripts/` рассчитаны на запуск в этом порядке, из корня
проекта:

```bash
# 1. Создать схему SQLite (идемпотентно).
uv run python scripts/init_db.py

# 2. Залить публикации ИТМО за период в БД.
uv run python scripts/populate_publications.py \
    --start-date 2025-01-01 --end-date 2026-05-01

# 3. По каждой публикации спросить у OpenAlex OA PDF и скачать его.
uv run python scripts/fetch_papers.py --limit 10

# 4. Прогнать скачанные PDF через регулярки и собрать кандидатные ссылки.
uv run python scripts/extract_repo_links.py
```

## База данных

| Таблица        | Что хранит                                                      |
| -------------- | ---------------------------------------------------------------- |
| `publications` | Одна строка на работу из OpenAlex; метаданные + `pdf_url` + `pdf_local_path` + `has_code` + `code_url`. |
| `repo_links`   | Одна строка на **кандидатный** URL, найденный в PDF: хост, окружающий текст, номер страницы, флаг `is_relevant`. |
| `persons_itmo`, `persons_external`, `publication_authors` | Авторы и их связь с публикациями, отдельно сотрудники ИТМО и внешние. |
| `departments`, `publication_departments` | Зарезервированы под будущий шаг — связку статей с подразделениями ИТМО. |

Поле `repo_links.is_relevant` пока всегда `NULL`. На этом этапе пайплайн
просто собирает все URL, чей хост похож на репозиторный.