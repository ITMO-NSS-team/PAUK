# `pauk/sources/` — HTTP-клиенты внешних API

**Что здесь:** общий HTTP-слой (retry/backoff) и по одному клиенту на
внешний API.

**Какие файлы задействует:** `pauk/sources/base.py`, `openalex.py`,
`github.py`, `crossref.py`, `orcid.py`, `openreview.py`.

Тонкие обёртки, каждая знает только свой API. Все наследуются от общего
`base.py::HttpClient` — раньше в проекте было 4 несогласованных
HTTP-стека, теперь один.

## `base.py::HttpClient`

- `_get(url, params=None, retries=3)` — общий retry-цикл: `429`/`5xx` →
  ждёт `Retry-After` из заголовка, если сервер его прислал, иначе
  экспоненциальная задержка (`min(60, 2**attempt)`); сетевые исключения
  (`requests.RequestException`) — та же экспонента, до `retries` попыток.
  Возвращает `requests.Response`.
- `get_json(url, params=None, retries=3)` — `_get(...).json()`.
- `get_bytes(url, retries=3)` — `_get(...).content`. Добавлен в этой
  сессии специально под скачивание PDF (`code_links.py`) — до этого в
  клиенте была только JSON-версия.

Оба метода параметризуемы по `retries`, что даёт вызывающему коду
управлять агрессивностью ретраев для конкретного случая (например,
health-check краулера в `code_links.py` идёт с `retries=0` — это не должно
задерживать весь прогон, если сервис лежит).

## Клиенты

| Файл | Класс | Что берёт |
|---|---|---|
| `openalex.py` | `OpenAlexClient` | `get_work`, `iter_works` (курсорная пагинация по ROR + диапазону дат), `get_author` |
| `github.py` | `GitHubClient` | `get_repository`, `has_readme` (отдельный вызов — основной payload репозитория наличие README не сообщает) |
| `crossref.py` | `CrossrefClient` | `get_work(doi)` — используется `PersonsStage` для backfill ORCID по фамилии |
| `orcid.py` | `OrcidClient` | `get_record(orcid)` |
| `openreview.py` | `OpenReviewClient` | `search(term)`, с ленивым логином (`_login()` только когда реально нужен токен, не в конструкторе) |

Ни один клиент не padает молча на отсутствующих кредах: `OpenReviewClient`
просто не логинится без `username`/`password` (см.
[pipeline/persons.md](pipeline/persons.md) — вызывающий код сам решает,
пропускать ли шаг), остальные не требуют авторизации вовсе или используют
`GITHUB_TOKEN`, если он задан.

## PDF-Crawler-Service — не в этом пакете

Fallback-скачивание PDF по DOI через внешний
[PDF-Crawler-Service](https://github.com/gurinboru/PDF-Crawler-Service)
живёт не здесь, а прямо в `pipeline/stages/code_links.py`
(`_crawler_available`/`_pdf_pages`) — это не клиент общего назначения,
а специфичная для одного стейджа логика поверх обычного `HttpClient`.
Подробности — [pipeline/code-links.md](pipeline/code-links.md) и
[deploy.md](deploy.md).
