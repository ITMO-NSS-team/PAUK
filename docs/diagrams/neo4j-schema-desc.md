# Описание схемы графа Neo4j

Пополевое описание узлов и связей — **реальная схема, как её строит код**
(`pauk/graph/extract.py::NODE_REGISTRY` + уникальные ключи из
`pauk/graph/schema.py::CONSTRAINTS`). Диаграмма той же схемы —
[`neo4j-schema.md`](neo4j-schema.md); контекст коннектора —
[`../architecture/neo4j-graph.md`](../architecture/neo4j-graph.md).

Отмечены (*Unique*) только те поля, на которых реально стоит констрейнт
уникальности. Поля, которых нет в `prop_fields` соответствующего `NodeSpec`,
в граф не попадают, даже если есть в pydantic-модели (см.
[`../architecture/models.md`](../architecture/models.md)).

## 1. Узлы (Nodes / Entities)

### 1. `Person`

> **Метка узла:** `«Node : Person | Itmo | External»`
> Исследователь / автор — из ИТМО (`:Person:Itmo`) или внешний (`:Person:External`).

* **`id`** (`ID`, *Unique*) — идентификатор автора (голый OpenAlex author ID).
* **`openalex_id`** (`String`) — идентификатор в OpenAlex.
* **`orcid`** (`String`) — идентификатор ORCID.
* **`name_en`** (`String`) — имя на английском.
* **`name_ru`** (`String`) — полное имя на русском.
* **`first_name_ru`** / **`second_name_ru`** / **`surname_ru`** (`String`) — имя / отчество / фамилия (только у ИТМО-персон).
* **`name_variants`** (`List[String]`) — варианты написания имени.
* **`email`** (`String`) — e-mail.
* **`degree`** (`String`) — учёная степень (только у ИТМО-персон).
* **`github`** / **`google_scholar`** / **`openreview`** / **`thesis`** (`String`) — ссылки на профили (только у ИТМО-персон).
* **`scopus_id`** / **`researcher_id`** / **`dblp_id`** (`String`) — внешние идентификаторы.
* **`homepage`** / **`gitlab_username`** / **`linkedin`** / **`twitter`** / **`wikipedia`** (`String`) — прочие ссылки.
* **`biography`** (`String`) — биография; **`country`** (`String`) — страна.
* **`works_count`** / **`cited_by_count`** / **`h_index`** / **`i10_index`** (`Integer`) — библиометрия.
* **`counts_by_year`** (`JSON`) — статистика по годам.
* **`affiliations`** (`JSON`) — сырые аффилиации; **`other_names`** (`List[String]`) — прочие имена.
* **`status`** (`String`), **`created_at`** / **`enriched_at`** (`Timestamp`) — служебные.
* **`merged_ids`** (`List[String]`) — id, схлопнутые в этот узел при дедупе.

---

### 2. `Publication`

> **Метка узла:** `«Node : Publication»`
> Научная публикация.

* **`id`** (`ID`, *Unique*) — идентификатор публикации (голый OpenAlex work ID).
* **`title`** (`String`) — название; **`type`** (`String`) — тип работы.
* **`fields`** (`List[String]`) — области; **`journal`** (`String`) — площадка издания.
* **`doi`** (`String`) — DOI; **`openalex_url`** (`String`) — ссылка в OpenAlex.
* **`publication_date`** (`Date`) / **`year`** (`Integer`) — дата / год.
* **`has_code`** (`Boolean`) / **`code_url`** (`String`) — найдена ли ссылка на код и какая.
* **`abstract`** (`String`) / **`full_text`** (`String`) — аннотация / полный текст (если извлечён из PDF).
* **`pdf_url`** (`String`), **`funding`** (`JSON`), **`versions`** (`JSON`) — прочее.
* **`merged_ids`** (`List[String]`) — схлопнутые при дедупе id.

---

### 3. `Department`

> **Метка узла:** `«Node : Department»`
> Подразделение ИТМО (мегафакультет, факультет, институт, центр, кафедра, лаборатория).

* **`id`** (`ID`, *Unique*) — идентификатор — человекочитаемый uid-слаг из `name_en`.
* **`name_en`** (`String`) — название на английском.
* **`name_ru`** (`String`) — название на русском.
* **`name_variants`** (`List[String]`) — варианты написания.
* **`kind`** (`String`) — уровень: `megafaculty | faculty | institute | center | department | lab`.
* **`parent_id`** (`String`) — uid родителя-`Department` (если подразделение вложено).
* **`organization_id`** (`String`) — uid `Organization` (если подразделение верхнего уровня).

Задан ровно один из `parent_id` / `organization_id` — это ребро `PART_OF` вверх по иерархии.

---

### 4. `Organization`

> **Метка узла:** `«Node : Organization»`
> Организация — корень орг-иерархии (ИТМО, а также со-аффилиации). Несколько
> организаций сосуществуют в графе как разные корни.

* **`id`** (`ID`, *Unique*) — uid-слаг.
* **`name_en`** (`String`, *Unique*) — название на английском.
* **`name_ru`** (`String`) — название на русском.
* **`ror_id`** (`String`) — идентификатор в реестре ROR (для ИТМО — `https://ror.org/04txgxn49`).
* **`country`** (`String`) — страна; **`type`** (`String`) — тип (`university`, …).

---

### 5. `Repository`

> **Метка узла:** `«Node : Repository»`
> Репозиторий с исходным кодом (GitHub).

* **`id`** (`ID`, *Unique*) — идентификатор (`github_owner_name`).
* **`url`** (`String`, *Unique*) — URL репозитория.
* **`name`** (`String`) — имя; **`github_id`** (`String`) — числовой id GitHub.
* **`cited_urls`** (`List[String]`) — URL-ы, которыми репозиторий цитировали до канонизации.
* **`description`** (`String`), **`license`** (`String`), **`has_readme`** (`Boolean`), **`stars_num`** (`Integer`).
* **`access_date`** / **`last_updated`** (`Date`) — дата проверки / последнего обновления.
* **`contributors`** (`List[String]`) — контрибьюторы; **`merged_ids`** (`List[String]`) — схлопнутые id.

---

### 6. `GitHubProfile`

> **Метка узла:** `«Node : GitHubProfile»`
> Аккаунт-владелец репозитория на GitHub (пользователь или организация).

* **`id`** (`ID`, *Unique*) — идентификатор аккаунта.
* **`login`** (`String`, *Unique*) — логин на GitHub.
* **`name`** (`String`) — отображаемое имя; **`html_url`** (`String`) — ссылка на профиль.
* **`description`** (`String`), **`location`** (`String`), **`type`** (`String`) — тип аккаунта.

---

### 7. `LinkCandidate`

> **Метка узла:** `«Node : LinkCandidate»`
> Кандидат code-ссылки, ещё не зарезолвившийся в `Repository` (заводится на лету;
> становится `Repository`, как только репозиторий успешно зарезолвлен).

* **`id`** (`ID`, *Unique*) — сам URL.
* **`url`** (`String`) — URL; **`host`** (`String`) — хост ссылки.

## 2. Связи (Relationships / Edges)

### 1. `AUTHORED`

> **Связывает:** `Person` → `Publication`. Авторство публикации.

* **`position`** (`Integer`) — позиция автора в списке.
* **`affiliation`** (`String`) — строка аффилиации в этой работе.
* **`affiliation_source`** (`String`) — откуда взята аффилиация.
* **`is_corresponding`** (`Boolean`) — автор для переписки.

---

### 2. `BELONGS_TO`

> **Связывает:** `Person:Itmo` → `Department`. Принадлежность автора подразделению
> (выведена сопоставлением аффилиаций с каталогом). Без свойств.

---

### 3. `CONTRIBUTED_TO`

> **Связывает:** `Person:Itmo` → `Repository`. Участие в разработке репозитория.

* **`role`** (`String`) — роль в разработке.

---

### 4. `PART_OF`

> **Связывает:** `Department` → `Department` либо `Department` → `Organization`.
> Рекурсивная орг-иерархия: подразделение входит в родителя — другое подразделение
> (`parent_id`) или корневую организацию (`organization_id`). Без свойств.

---

### 5. `PRODUCED_BY`

> **Связывает:** `Publication` → `Department`. Публикация произведена подразделением
> (по департаментам её ИТМО-авторов). Без свойств.

---

### 6. `MENTIONS_LINK`

> **Связывает:** `Publication` → `Repository` либо `Publication` → `LinkCandidate`.
> Упоминание code-ссылки в тексте/абстракте публикации.

* **`context`** (`List[String]`) — фрагменты текста вокруг ссылки.
* **`page_number`** (`List[Integer]`) — страницы (`0` = абстракт: Neo4j не хранит `null` в массиве-свойстве, поэтому сентинел не `None`).
* **`is_relevant`** (`Boolean`), **`llm_confidence`** (`Float`), **`llm_reason`** (`String`) — вердикт LLM.

---

### 7. `DEVELOPED_BY`

> **Связывает:** `Repository` → `Department`. Репозиторий разрабатывается подразделением
> (по департаментам его ИТМО-контрибьюторов). Без свойств.

---

### 8. `IMPLEMENTS`

> **Связывает:** `Repository` → `Publication`. Репозиторий реализует публикацию. Без свойств.

---

### 9. `OWNED_BY`

> **Связывает:** `Repository` → `GitHubProfile`. Владелец репозитория. Без свойств.
