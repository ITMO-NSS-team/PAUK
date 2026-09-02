# `pauk dedup departments` — дедуп департаментов в графе

**Что здесь:** как схлопываются дубли узлов `Department` без ручного словаря
`aliases`.

**Какие файлы задействует:** `pauk/graph/dept_dedup/` (`normalize.py`,
`matching.py`, `embeddings.py`, `adjudicate.py`, `pipeline.py`),
`pauk/graph/client.py` (`fetch_departments_for_dedup`,
`merge_department_nodes_batch`).

Сейчас группировка написаний департамента держится на поле `aliases` в
`data/static/departments_catalog.json` (в графе — `Department.name_variants`),
которое ведётся руками (см. [pipeline/departments.md](pipeline/departments.md)).
`pauk dedup departments` — автоматическая замена: воронка из детерминированных
этапов плюс LLM-арбитраж неоднозначных пар.

Отдельная команда, не часть обычного прогона и не часть `pauk dedup graph`
(тот схлопывает персон/публикации/репозитории). По умолчанию применяет
результат; `--dry-run` только считает и пишет журнал.

## Воронка

| Этап | Модуль | Что делает |
|---|---|---|
| 0 — нормализация | `normalize.py` | `raw` → нормальная строка + токены + **домен-токены** (без слов-квалификаторов `center/faculty/international/...`) + акроним + язык. Гомоглифы кир./лат., стоп-слова, грубый стемминг, транслитерация. |
| 1 — блокинг | `matching.py::block` (+ опц. `embeddings.py`) | все узлы → пары-кандидаты: общий stem-токен, перекрытие char-4-грамм ≥ 0.30, совпадение акронима, либо (если включён эмбеддер) соседи по косинусу LaBSE ≥ 0.70. Слишком общие токены пропускаются. |
| 2 — сигналы + полосы | `matching.py::score_pair`, `assign_band` | лексика (`token_set/sort_ratio` на `difflib`, Levenshtein-ratio, char-3–5-gram косинус), Jaccard по токенам и домену, флаг акронима, **guard**: непустая симметрическая разность домен-лемм (`head_diff`) запрещает авто-слияние; `kinds_compatible` (megafaculty / school-faculty / center-institute / lab-department — между классами не сливаем). Полоса: `auto-merge` / `auto-reject` / `llm`. |
| 4 — LLM-арбитраж | `adjudicate.py` | только полоса `llm`. Промпт: два названия + контекст (`kind`, родитель, общие сотрудники и публикации, общий ли родитель). Ответ JSON `{relation: same\|parent_child\|sibling\|unrelated, confidence, reason}`. Сливается только `same` при `confidence ≥ 0.8`; `parent_child` → строка `part_of_suggested` в журнале (ребро не трогается); остальное — `held`. |
| 5 — кластеризация | `pipeline.py` | union-find по принятым парам (`_grouped` из `pipeline/stages/dedup.py`). Проверка группы: охват >1 класса `kind` или пара, признанная LLM разными, → вся группа в `held`. |
| 6 — применение | `pipeline.py::_apply` | канонический узел на группу — самый полный (`name_en`+`name_ru`), затем самый связанный, затем меньший `id`. Написания дублей → `canonical.name_variants` (`upsert_nodes_batch`), затем `merge_department_nodes_batch` переносит рёбра `PART_OF`/`BELONGS_TO`/`PRODUCED_BY`/`DEVELOPED_BY` и удаляет дубль. `merged_ids` остаётся на каноническом — поздний `publish graph` от старой группы перефолдит id сам. |

## Детерминированность

Этапы 0–2 — чистые функции. Эмбеддер (если включён) пиннится по имени модели.
Вердикты LLM кэшируются в MongoDB (`dept_dedup_verdicts`, ключ — sha1 от
`prompt_version|model|нормализованная пара`), полный лог — `llm_logs_dept_dedup`
(см. [storage.md](storage.md)). Повторный прогон бесплатен и воспроизводим,
LLM зовётся только на новых парах. Union-find порядко-независим.

## Настройки

- `OPENROUTER_API_KEY`, `PAUK_LLM_MODEL` (`.env`) — как у остальных LLM-этапов.
  Без ключа полоса `llm` целиком уходит в `held`.
- `--embedder <labse|minilm|id модели sentence-transformers>` — флаг команды,
  не env: включает семантический блокинг. По умолчанию не задан — лексический
  блокинг без зависимости от `sentence-transformers`/torch. Библиотека
  намеренно не в зависимостях проекта; ставится вручную.

## Журнал

`data/cache/dedup_candidates_departments.jsonl` (`AtomicWriter`): строки
`merged` (`merged_into`, `rule`), `part_of_suggested`, `held` (`held_because`,
`reason`). Ни одна эвристика не молчит.

## Известные ограничения

- Голые акронимы без раскрытия (`FBIT`, `PhysNano Department`) и RU↔EN-пары
  ниже порога LaBSE не становятся кандидатами — их не с чем сравнить.
- `_fold_nodes_batch` делает `MERGE (canonical)-[:PART_OF]->(parent)`: если у
  дубля был другой родитель, у канонического узла окажется два `PART_OF`.
  На практике дубли делят родителя; расхождение видно в графе и правится
  вручную.
