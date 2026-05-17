# PAUK — мониторинг публикаций ИТМО

Пайплайн, который для каждой публикации сотрудников ИТМО ищет ссылку на
репозиторий с кодом, выложенный самими авторами:

`OpenAlex -> SQLite -> PDF + Abstract -> regex -> LLM -> publications.code_url`

Подробный отчёт о решении (мотивация, схема БД, метрики, известные
ограничения) — в [REPORT.md](REPORT.md).

## Установка

```bash
uv sync
cp .env.example .env
# отредактировать .env
```

## Запуск пайплайна

Скрипты запускаются из корня проекта **в этом порядке**:

```bash
# 1. Создать схему SQLite (идемпотентно).
uv run python scripts/init_db.py

# 2. Загрузить публикации ИТМО из OpenAlex за период.
uv run python scripts/populate_publications.py \
    --start-date 2025-01-01 --end-date 2026-05-01

# 3. По каждой публикации забрать абстракт и (если доступно) скачать PDF.
uv run python scripts/fetch_papers.py --limit 50

# 4. Извлечь кандидатные ссылки на репозитории из PDF и абстрактов.
uv run python scripts/extract_repo_links.py

# 5. Прогнать кандидатов через LLM: репозиторий авторов или чужой?
uv run python scripts/classify_repo_links.py --limit 200

# 6. Прокинуть подтверждённые ссылки в publications.has_code/code_url.
uv run python scripts/sync_publications.py
```

Шаги 3 и 5 принимают `--limit`, чтобы обрабатывать порциями. Все шаги
идемпотентны: повторный запуск не сломает уже сохранённые данные.

## Структура

```
PAUK/
├── scripts/
│   ├── config.py                # пути, эндпоинты, загрузка .env
│   ├── init_db.py
│   ├── populate_publications.py
│   ├── fetch_papers.py
│   ├── extract_repo_links.py
│   ├── classify_repo_links.py
│   └── sync_publications.py
├── data/                        # .gitignore
│   ├── itmo_research_opensource.db
│   └── pdfs/
├── .env                         # .gitignore
├── .env.example                 # шаблон для .env
├── README.md
└── REPORT.md                    # подробный отчёт о решении
```

## Чтение результата из БД

`publications.code_url` хранит **JSON-массив** подтверждённых LLM ссылок,
отсортированный от максимальной уверенности к минимальной (если найдено
несколько репо к одной статье).

```python
import json
import sqlite3

conn = sqlite3.connect("data/itmo_research_opensource.db")
rows = conn.execute(
    "SELECT id, title, code_url FROM publications WHERE has_code = 1"
)
for pub_id, title, code_url_json in rows:
    urls = json.loads(code_url_json)
    print(f"{pub_id}: {title}")
    for url in urls:
        print(f"  {url}")
```

Полный список кандидатов с контекстами и причинами вердикта LLM лежит
в таблице `repo_links` — оттуда удобно смотреть пограничные случаи.
