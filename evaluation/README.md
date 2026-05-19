# Ручная оценка качества пайплайна

## Шаг 1. Сформировать выборку

```bash
uv run python scripts/sample_for_review.py
```

По умолчанию: **80 публикаций**, случайно выбранных из всех, у которых
есть хоть какой-то материал (PDF или абстракт). С гарантией:
- минимум **5 публикаций с подтверждённой авторской ссылкой**
  (`has_code = 1`);
- минимум **5 публикаций с кандидатами, которые LLM пометил как чужие**
  (нашли ссылки, но все `is_relevant = 0`);
- остальные — случайные, в том числе те, у кого вообще не нашлось
  кандидатов.

Фильтр по типу материала — `--material`:
- `pdf` — только публикации со скачанным PDF;
- `abstract` — только с абстрактом;
- `both` — у которых есть **и** PDF, **и** абстракт;
- `all` — у кого есть хоть что-то (по умолчанию).

Фильтр по итоговому вердикту пайплайна — `--status`:
- `confirmed` — только `has_code = 1` (есть подтверждённый авторский репо);
- `rejected` — только те, где кандидаты есть, но все отклонены LLM;
- `any` — все (с гарантией минимумов 5+5, по умолчанию).

При `--status confirmed` / `--status rejected` `--min-confirmed` /
`--min-rejected` не действуют — выборка идёт только из одной группы.

Другие опции: `--size`, `--min-confirmed`, `--min-rejected`, `--seed`,
`--output`.

## Шаг 2. Что лежит в `evaluation/sample.jsonl`

Одна публикация на строку:

```json
{
  "publication_id": "...",
  "title": "...",
  "doi": "...",
  "openalex_url": "...",
  "authors": "...",
  "abstract_preview": "...",
  "pdf_url": "...",
  "pdf_local_path": "...",
  "current_has_code": 0,
  "current_code_url": null,

  "candidates": [
    {
      "url": "...",
      "host": "...",
      "page_number": 23,
      "context": "...200 символов из статьи...",
      "llm_verdict": "ДА",
      "is_relevant": 1,
      "llm_confidence": 0.95,
      "llm_reason": "...",
      "manual_correct": null
    }
  ],

  "manual_review": {
    "really_has_code": null,
    "actual_repo_urls": [],
    "comment": ""
  }
}
```