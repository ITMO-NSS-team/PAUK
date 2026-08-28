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

> **Метка узла:** `«Node : Person»`
> Исследователь / автор — из ИТМО или внешний, различается свойством `is_itmo`,
> а не меткой.

* **`id`** (`ID`, *Unique*) — идентификатор автора (голый OpenAlex author ID).
* **`is_itmo`** (`Boolean`) — принадлежность к ИТМО. «Липкое» свойство: раз
  выставленное в `true`, никогда не понижается до `false` более поздней
  external-строкой того же человека.
* **`openalex_id`** (`String`) — идентификатор в OpenAlex.
* **`orcid`** (`String`) — идентификатор ORCID.
* **`name_en`** (`String`) — полное имя на английском (собрано из частей ниже).
* **`first_name_en`** (`String`) — имя на английском.
* **`second_name_en`** (`String`) — отчество на английском (если известно).
* **`surname_en`** (`String`) — фамилия на английском.
* **`name_variants`** (`List[String]`) — варианты написания имени.
* **`name_ru`** (`String`) — полное имя на русском.
* **`first_name_ru`** (`String`) — имя на русском (только у ИТМО-персон).
* **`second_name_ru`** (`String`) — отчество на русском (только у ИТМО-персон).
* **`surname_ru`** (`String`) — фамилия на русском (только у ИТМО-персон).
* **`email`** (`String`) — адрес e-mail.
* **`degree`** (`String`) — учёная степень (только у ИТМО-персон).
* **`github`** (`String`) — профиль GitHub (только у ИТМО-персон).
* **`google_scholar`** (`String`) — профиль Google Scholar (только у ИТМО-персон).
* **`openreview`** (`String`) — профиль OpenReview (только у ИТМО-персон).
* **`thesis`** (`String`) — диссертация / квалификационная работа (только у ИТМО-персон).
* **`scopus_id`** (`String`) — идентификатор Scopus.
* **`researcher_id`** (`String`) — Web of Science ResearcherID.
* **`dblp_id`** (`String`) — идентификатор dblp.
* **`other_names`** (`List[String]`) — другие имена / псевдонимы.
* **`biography`** (`String`) — биография.
* **`country`** (`String`) — страна.
* **`homepage`** (`String`) — личный веб-сайт.
* **`gitlab_username`** (`String`) — имя пользователя GitLab.
* **`linkedin`** (`String`) — профиль LinkedIn.
* **`twitter`** (`String`) — профиль Twitter/X.
* **`wikipedia`** (`String`) — страница в Википедии.
* **`works_count`** (`Integer`) — количество работ.
* **`cited_by_count`** (`Integer`) — общее количество цитирований.
* **`h_index`** (`Integer`) — индекс Хирша.
* **`i10_index`** (`Integer`) — i10-индекс.
* **`counts_by_year`** (`JSON`) — статистика по годам.
* **`status`** (`String`) — статус сотрудника / исследователя.
* **`created_at`** (`Timestamp`) — время создания записи.
* **`enriched_at`** (`Timestamp`) — время последнего обогащения данных.
* **`affiliations`** (`JSON`) — сырые аффилиации.
* **`merged_ids`** (`List[String]`) — id, схлопнутые в этот узел при дедупе.

---

### 2. `Publication`

> **Метка узла:** `«Node : Publication»`
> Научная публикация.

* **`id`** (`ID`, *Unique*) — идентификатор публикации (голый OpenAlex work ID).
* **`title`** (`String`) — название публикации.
* **`type`** (`String`) — тип работы.
* **`fields`** (`List[String]`) — области знаний.
* **`journal`** (`String`) — площадка издания.
* **`doi`** (`String`) — DOI статьи.
* **`publication_date`** (`Date`) — дата публикации.
* **`year`** (`Integer`) — год публикации.
* **`has_code`** (`Boolean`) — найдена ли ссылка на код.
* **`code_url`** (`String`) — ссылка на код.
* **`funding`** (`JSON`) — информация о финансировании.
* **`openalex_url`** (`String`) — ссылка на публикацию в OpenAlex.
* **`pdf_url`** (`String`) — ссылка на PDF.
* **`abstract`** (`String`) — аннотация.
* **`full_text`** (`String`) — полный текст (если извлечён из PDF).
* **`versions`** (`JSON`) — версии публикации.
* **`merged_ids`** (`List[String]`) — id, схлопнутые при дедупе.

---

### 3. `Department`

> **Метка узла:** `«Node : Department»`
> Подразделение ИТМО (мегафакультет, факультет, институт, центр, кафедра, лаборатория).

* **`id`** (`ID`, *Unique*) — идентификатор: человекочитаемый uid-слаг из `name_en`.
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
* **`country`** (`String`) — страна.
* **`type`** (`String`) — тип организации (`university`, …).

---

### 5. `Repository`

> **Метка узла:** `«Node : Repository»`
> Репозиторий с исходным кодом (GitHub).

* **`id`** (`ID`, *Unique*) — идентификатор (`github_owner_name`).
* **`name`** (`String`) — имя репозитория.
* **`url`** (`String`, *Unique*) — URL репозитория.
* **`github_id`** (`String`) — числовой id GitHub.
* **`cited_urls`** (`List[String]`) — URL-ы, которыми репозиторий цитировали до канонизации.
* **`description`** (`String`) — описание.
* **`access_date`** (`Date`) — дата проверки / получения доступа.
* **`has_readme`** (`Boolean`) — наличие README.
* **`stars_num`** (`Integer`) — количество звёзд.
* **`last_updated`** (`Date`) — дата последнего обновления.
* **`license`** (`String`) — лицензия.
* **`contributors`** (`List[String]`) — контрибьюторы.
* **`merged_ids`** (`List[String]`) — id, схлопнутые при дедупе.

---

### 6. `GitHubProfile`

> **Метка узла:** `«Node : GitHubProfile»`
> Аккаунт-владелец репозитория на GitHub (пользователь или организация).

* **`id`** (`ID`, *Unique*) — идентификатор аккаунта.
* **`login`** (`String`, *Unique*) — логин на GitHub.
* **`name`** (`String`) — отображаемое имя.
* **`html_url`** (`String`) — ссылка на профиль.
* **`description`** (`String`) — описание профиля.
* **`location`** (`String`) — местоположение.
* **`company`** (`String`) — место работы, указанное в профиле; используется как
  дополнительный признак при сопоставлении аккаунта с автором.
* **`type`** (`String`) — тип аккаунта (пользователь / организация).

---

### 7. `LinkCandidate`

> **Метка узла:** `«Node : LinkCandidate»`
> Кандидат code-ссылки, ещё не зарезолвившийся в `Repository` (заводится на лету;
> становится `Repository`, как только репозиторий успешно зарезолвлен).

* **`id`** (`ID`, *Unique*) — сам URL.
* **`url`** (`String`) — URL ссылки.
* **`host`** (`String`) — хост ссылки.

## 2. Связи (Relationships / Edges)

### 1. `AUTHORED` (Авторство)

> **Связывает:** `Person` → `Publication`. Авторство публикации.

* **`position`** (`Integer`) — позиция автора в списке.
* **`affiliation`** (`String`) — строка аффилиации в этой работе.
* **`affiliation_source`** (`String`) — откуда взята аффилиация.
* **`is_corresponding`** (`Boolean`) — автор для переписки.

---

### 2. `BELONGS_TO` (Принадлежность подразделению)

> **Связывает:** `Person {is_itmo: true}` → `Department`. Принадлежность автора
> подразделению (выведена сопоставлением аффилиаций с каталогом). Без свойств.

---

### 3. `CONTRIBUTED_TO` (Участие в разработке)

> **Связывает:** `Person {is_itmo: true}` → `Repository`. Участие в разработке
> репозитория.

* **`role`** (`String`) — роль в разработке.

---

### 4. `PART_OF` (Иерархия подразделений)

> **Связывает:** `Department` → `Department` либо `Department` → `Organization`.
> Рекурсивная орг-иерархия: подразделение входит в родителя — другое подразделение
> (`parent_id`) или корневую организацию (`organization_id`). Без свойств.

---

### 5. `PRODUCED_BY` (Публикация подразделения)

> **Связывает:** `Publication` → `Department`. Публикация произведена подразделением
> (по департаментам её ИТМО-авторов). Без свойств.

---

### 6. `MENTIONS_LINK` (Упоминание code-ссылки)

> **Связывает:** `Publication` → `Repository` либо `Publication` → `LinkCandidate`.
> Упоминание code-ссылки в тексте / абстракте публикации.

* **`context`** (`List[String]`) — фрагменты текста вокруг ссылки.
* **`page_number`** (`List[Integer]`) — страницы (`0` = абстракт: Neo4j не хранит `null` в массиве-свойстве, поэтому сентинел не `None`).
* **`is_relevant`** (`Boolean`) — вердикт релевантности.
* **`llm_confidence`** (`Float`) — уверенность LLM.
* **`llm_reason`** (`String`) — обоснование LLM.

---

### 7. `DEVELOPED_BY` (Разработка подразделением)

> **Связывает:** `Repository` → `Department`. Репозиторий разрабатывается подразделением
> (по департаментам его ИТМО-контрибьюторов). Без свойств.

---

### 8. `IMPLEMENTS` (Реализация публикации)

> **Связывает:** `Repository` → `Publication`. Репозиторий реализует публикацию. Без свойств.

---

### 9. `OWNED_BY` (Владение)

> **Связывает:** `Repository` → `GitHubProfile`. Владелец репозитория. Без свойств.
