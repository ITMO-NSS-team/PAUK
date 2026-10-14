"""The two LLM calls: parsing the question (№1) and wording the answer (№2).

Neither call reaches the graph. №1 only fills a validated plan; №2 only
rewords rows the code already selected, and every link it may use comes
from those rows, so a link it invents is stripped before the answer leaves.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from pauk.settings import Settings
from pauk.sources.llm import OpenRouterClient

from .plan import FIELDS, QueryPlan, fallback_plan

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def make_client(config: Settings) -> OpenRouterClient:
    return OpenRouterClient(config.request_timeout, config.openrouter_api_key, config.llm_model,
                            config.openrouter_proxy_url)


def _period(year_from: int | None = None, year_to: int | None = None) -> dict[str, Any]:
    return {"only_itmo": True, "year_from": year_from, "year_to": year_to}

PARSE_EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    # --- 6-10: a topic, optionally with a department, code or period
    ("Кто в ИТМО занимается метаматериалами?",
     {"template": "6", "params": {"target": "persons"}, "entities": [],
      "core": ["метаматериалы", "metamaterials", "electromagnetic metamaterials"], "filter": _period()}),
    ("Какие статьи вышли про машинный перевод?",
     {"template": "7", "params": {"target": "publications"}, "entities": [],
      "core": ["машинный перевод", "machine translation", "neural machine translation"], "filter": _period()}),
    ("Кто из факультета фотоники работал над фотонными кристаллами?",
     {"template": "8", "params": {"target": "persons"},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}],
      "core": ["фотонные кристаллы", "photonic crystals"], "filter": _period()}),
    ("Есть ли открытый код у работ по обработке спутниковых снимков?",
     {"template": "9", "params": {"target": "publications", "metric": "exists", "require_code": True,
                                  "link": "mentions"},
      "entities": [], "core": ["обработка спутниковых снимков", "satellite image processing", "remote sensing"],
      "filter": _period()}),
    ("Какие публикации про машинный перевод имеют репозиторий с лицензией Apache-2.0?",
     {"template": "9", "params": {"target": "publications", "require_code": True, "link": "mentions",
                                  "repo_filters": [{"field": "license", "op": "eq", "value": "Apache-2.0"}]},
      "entities": [], "core": ["машинный перевод", "machine translation", "neural machine translation"],
      "filter": _period()}),
    ("Кто публиковался по метаматериалам за последние 4 года?",
     {"template": "10", "params": {"target": "persons", "last_n_years": 4}, "entities": [],
      "core": ["метаматериалы", "metamaterials", "electromagnetic metamaterials"], "filter": _period()}),
    ("Какие работы по машинному переводу вышли в 2023–2024 годах?",
     {"template": "10", "params": {"target": "publications"}, "entities": [],
      "core": ["машинный перевод", "machine translation", "neural machine translation"],
      "filter": _period(2023, 2024)}),
    # --- 1: people by attributes
    ("Кто из факультета фотоники не имеет учёной степени?",
     {"template": "1", "params": {"target": "persons", "metric": "list",
                                  "filters": [{"field": "degree", "op": "is_null"}]},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    ("Сколько кандидатов и докторов наук на факультете фотоники по отдельности?",
     {"template": "1", "params": {"target": "persons", "metric": "count", "group_by": "degree",
                                  "filters": [{"field": "degree", "op": "in", "value": ["candidate", "doctor"]}]},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    ("Сколько сотрудников ИТМО имеют степень доктора физико-математических наук?",
     {"template": "1", "params": {"target": "persons", "metric": "count",
                                  "filters": [{"field": "degree", "op": "eq", "value": "д.ф.-м.н."},
                                              {"field": "is_itmo", "op": "eq", "value": True}]},
      "entities": [], "core": [], "filter": _period()}),
    ("Сколько авторов записано без подразделения?",
     {"template": "1", "params": {"target": "persons", "metric": "count", "no_dept": True},
      "entities": [], "core": [], "filter": _period()}),
    # --- 2: publications
    ("Какая доля публикаций с открытым PDF у факультета фотоники?",
     {"template": "2", "params": {"target": "publications", "metric": "ratio",
                                  "ratio_filters": [{"field": "pdf_urls", "op": "not_null"}]},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    ("Сколько публикаций выходило по годам за последние 6 лет у факультета фотоники?",
     {"template": "2", "params": {"target": "publications", "metric": "count", "group_by": "year",
                                  "last_n_years": 6},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    # --- 3: co-authorship (the subtype follows from the entities, it is not written)
    ("Сколько раз Лебедев А. В. и Орлова Н. С. публиковались вместе?",
     {"template": "3", "params": {"target": "publications", "metric": "count"},
      "entities": [{"kind": "person", "name": "Лебедев А. В."}, {"kind": "person", "name": "Орлова Н. С."}],
      "core": [], "filter": _period()}),
    ("С кем чаще всего публикуется Морозов К. А.?",
     {"template": "3", "params": {"target": "persons", "metric": "list", "order": "desc", "limit": 1},
      "entities": [{"kind": "person", "name": "Морозов К. А."}], "core": [], "filter": _period()}),
    ("Кто из факультета фотоники публиковался в соавторстве с авторами из факультета биотехнологий?",
     {"template": "3", "params": {"target": "persons", "metric": "list"},
      "entities": [{"kind": "department", "name": "Факультет фотоники", "role": "subject"},
                   {"kind": "department", "name": "Факультет биотехнологий", "role": "partner"}],
      "core": [], "filter": _period()}),
    ("Есть ли у Морозова К. А. совместные публикации с кем-нибудь из факультета биотехнологий?",
     {"template": "3", "params": {"target": "publications", "metric": "exists"},
      "entities": [{"kind": "person", "name": "Морозов К. А."},
                   {"kind": "department", "name": "Факультет биотехнологий"}],
      "core": [], "filter": _period()}),
    ("У кого из сотрудников факультета фотоники больше всего соавторов не из ИТМО?",
     {"template": "3", "params": {"target": "persons", "metric": "list", "coauthor_is_itmo": False,
                                  "order": "desc", "limit": 1},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    # --- 4: repositories
    ("Кто из ИТМО контрибьютил в репозитории на языке Rust?",
     {"template": "4", "params": {"target": "persons", "metric": "list",
                                  "filters": [{"field": "language", "op": "eq", "value": "Rust"}]},
      "entities": [], "core": [], "filter": _period()}),
    ("Какие репозитории факультета фотоники не обновлялись больше пяти лет?",
     {"template": "4", "params": {"target": "repositories", "metric": "list",
                                  "filters": [{"field": "last_updated", "op": "lt", "value": {"years_ago": 5}}]},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    ("Сколько репозиториев факультета фотоники имеют больше 5 форков?",
     {"template": "4", "params": {"target": "repositories", "metric": "count",
                                  "filters": [{"field": "forks_num", "op": "gt", "value": 5}]},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    # --- 5: chains (three fixed patterns)
    ("Кто из факультета фотоники является автором статьи и контрибьютором её репозитория?",
     {"template": "5", "params": {"target": "persons", "pattern": "author_contributor"},
      "entities": [{"kind": "department", "name": "Факультет фотоники"}], "core": [], "filter": _period()}),
    # --- none: the original pipeline (profiles and topic search) takes over
    ("Сколько публикаций у Лебедева А. В. с 2024 года?",
     {"template": "none", "params": {}, "entities": [{"kind": "person", "name": "Лебедев А. В."}], "core": [],
      "expected_values": ["counts", "publications"], "fields": [], "filter": _period(2024),
      "graph_type": "person_profile"}),
    ("С какими зарубежными организациями чаще всего соавторствует факультет фотоники?",
     {"template": "none", "params": {}, "entities": [{"kind": "department", "name": "Факультет фотоники"}],
      "core": [], "expected_values": ["departments", "publications"], "fields": [], "filter": _period(),
      "graph_type": "department_profile"}),
]

_PARSE_RULES = """Ты разбираешь вопросы к графу научных данных Университета ИТМО.
В графе есть: публикации (название, аннотация, год, тип, DOI, журнал, финансирование, ссылки на PDF), авторы
(сотрудники ИТМО и внешние соавторы; у сотрудников есть учёная степень и профили GitHub, Google Scholar,
LinkedIn, ORCID, личный сайт), подразделения (факультеты, институты, лаборатории, иерархия), GitHub-репозитории
(лицензия, звёзды, форки, язык, дата последнего коммита, владелец) и их контрибьюторы.

Твоя задача: отнести вопрос к одному из 10 шаблонов запросов (или к "none") и заполнить параметры шаблона.
Запрос к графу строит код, а не ты: ты только выбираешь шаблон и параметры.

Верни ТОЛЬКО JSON-объект с полями:
- "template": "1", "2", ... "10" или "none" (строкой). Правила выбора ниже.
- "params": объект с параметрами шаблона. Для "none" — {}. Ключи со значением null не пиши.
- "entities": конкретные объекты, НАЗВАННЫЕ в вопросе по имени: [{"kind": "person"|"department", "name": "..."}].
  Имя человека пиши как в вопросе («Иванов Иван», «Бухановский А. В.»), название подразделения — в именительном
  падеже, аббревиатуры оставляй как в вопросе. Пусто, если никто не назван.
  Поле "role" ("subject" или "partner") пиши только у подразделений в шаблоне 3, когда подразделений два:
  "subject" — чьих людей или публикации возвращает вопрос («Кто из X ...» — X), "partner" — с кем сравнивают.
  Подразделение рядом с человеком всегда партнёр, роль у него не пиши.
- "core": ключевые словосочетания ТЕМЫ для семантического поиска — для понимания контекста необходимо.
  Пиши и по-русски, и по-английски (тексты публикаций на английском), 1–4 фразы. Нужен для шаблонов 6–10 и для
  "none", если тема есть; для шаблонов 1–5 — пустой список. Только то, что отличает тему: названия методов, моделей,
  задач, областей. Не добавляй отдельно общие слова вроде «архитектуры», «методы», «подходы», «модели», «системы»,
  «исследования», «применение» — они совпадают с чем угодно. Названия моделей и методов (ResNet, RT-DETR, BERT)
  пиши как есть, без перевода.
- "filter": {"only_itmo": true|false, "year_from": число|null, "year_to": число|null}.
  only_itmo=true по умолчанию; false, только если явно просят внешних соавторов (в шаблоне 3 для этого есть
  params.coauthor_is_itmo). Годы пиши сюда, а не в params. Границы включительно:
  «с 2022 года» — year_from 2022; «после 2021» — year_from 2022; «до 2020» — year_to 2019;
  «в 2023 году» — year_from 2023 и year_to 2023; «в 2023–2024 годах» — year_from 2023 и year_to 2024.
  «За последние N лет» сюда не пиши: для него есть params.last_n_years.
Только для "none" добавь ещё три поля (для остальных шаблонов их не пиши):
- "expected_values": какие разделы нужны в ответе, из
  ["persons", "departments", "publications", "repositories", "counts"].
  "counts" — для вопросов «сколько», статистики. Не включай разделы, которые явно исключены («без репозиториев»).
- "fields": какие поля показать, если пользователь явно ограничил; иначе []. Допустимые: <<FIELDS>>
- "graph_type": "topic_subgraph" (поиск по теме), "person_profile" (вопрос про конкретного автора),
  "department_profile" (вопрос про конкретное подразделение).
Не пиши "subtype": подтип шаблона 3 определяет код по entities.

ШАБЛОНЫ
"1" Люди: фильтр, подсчёт или группировка людей по подразделению и их атрибутам (степень, GitHub, Google Scholar,
    LinkedIn, ORCID, личный сайт, другие имена).
"2" Публикации: подсчёт, доля, среднее или группировка публикаций по подразделению, годам, типу, DOI,
    финансированию, открытому PDF.
"3" Соавторство: общие соавторы или публикации людей, соавторы одного человека, связи двух подразделений,
    человек и подразделение, рейтинг людей подразделения по соавторам.
"4" Репозитории: фильтр, подсчёт или группировка репозиториев и их контрибьюторов по лицензии, звёздам, форкам,
    языку, архивности, дате обновления, владельцу, роли, подразделению или человеку.
"5" Цепочки из нескольких связей между людьми, публикациями и репозиториями; только три паттерна (см. ниже).
"6" Кто автор публикаций по теме.
"7" Какие публикации есть по теме.
"8" Тема и подразделение.
"9" Тема и наличие кода.
"10" Тема и период.
"none" Ничего из этого не подходит: вопрос про профиль конкретного человека или подразделения («чем занимается»,
    «сколько публикаций у X»), глобальные рейтинги без якоря («у каких двух сотрудников больше всего совместных
    публикаций»), внешние организации, цепочки соавторства, «ни разу не публиковался», сравнение периодов
    («прирост 2025 к 2024»), всё, что нельзя точно выразить параметрами ниже.

КАК ВЫБРАТЬ ШАБЛОН
- Если в вопросе есть тема (её ты кладёшь в core), это шаблон 6–10. Если нужен код, репозиторий или реализация —
  "9"; иначе если названо подразделение — "8"; иначе если задан период — "10"; иначе «кто» — "6", а «какие
  публикации, работы, статьи» — "7". Если ограничений несколько, выбирай по этому порядку, а остальные всё равно
  запиши (подразделение в entities, период в filter или last_n_years, условия на репозиторий в repo_filters).
- Если темы нет, выбирай между 1–5 по смыслу вопроса.
- Если часть условия вопроса нельзя выразить параметрами, выбирай "none". Не отбрасывай условие молча: неполный
  шаблон хуже, чем "none".

ПАРАМЕТРЫ (params)
Имён людей и подразделений, темы и годов в params нет: имена в entities, тема в core, годы в filter.
Общие ключи:
- "target": что возвращается или считается: "persons", "publications" или "repositories". Обязателен для шаблонов 1–10.
- "metric": "list" (кто, какие, где), "count" (сколько), "exists" (есть ли), "ratio" (доля),
  "avg_per_year" (в среднем в год), "avg_per_person" (в среднем на человека). Обязателен для шаблонов 1–4.
  В шаблонах 5–10 пиши только "exists" для «есть ли», иначе не указывай.
- "filters": условия [{"field": "...", "op": "...", "value": ...}]. Операции: eq, ne, gt, gte, lt, lte, in,
  is_null, not_null. У is_null и not_null value нет; у in value — список.
- "group_by": "dept" (по подразделениям), "year", "quarter", "type" (по типу публикации) или "degree".
- "order": "desc" или "asc" и "limit": число. Суперлатив в единственном числе («самый», «больше всего»,
  «чаще всего», «у кого») — limit 1; «топ-5», «5 подразделений» — limit 5.
- "include_subunits": false — только если вопрос явно ограничивает «только самим подразделением»; иначе не пиши
  (подразделения ниже по иерархии учитываются сами).
- "last_n_years": число N для «за последние N лет»; годы в filter тогда не пиши.

Шаблон 1 (люди), target "persons":
- Поля filters: degree, github, google_scholar, linkedin, orcid, homepage, other_names, is_itmo.
- Подразделение — в entities. Если подразделения нет, а вопрос про «ИТМО», добавь условие is_itmo eq true.
- «Не указан ORCID», «нет профиля» — is_null; «указал профиль», «есть аккаунт» — not_null.
  «Не сотрудники ИТМО» — is_itmo eq false.
- Степень в графе хранится по-русски. Если сказано только «кандидат наук» или «доктор наук», пиши
  "candidate" или "doctor". Если названа область, пиши значение из графа: "к.т.н.", "д.т.н.", "к.ф.-м.н.",
  "д.ф.-м.н." и т. п. «Имеет степень» — not_null, «без степени» — is_null. «Кандидатов и докторов по
  отдельности» — group_by "degree" и filters degree in ["candidate", "doctor"].
- "no_dept": true — для вопросов «у скольких авторов не указано подразделение или место работы»
  (авторы без подразделения; entities пусты).
- «В каждом подразделении» — group_by "dept".

Шаблон 2 (публикации), target "publications":
- Поля filters: type (тип работы, например "preprint", "conference-paper"), doi, funding (грантовое
  финансирование — not_null), pdf_urls (открытый PDF — not_null), journal. «Без DOI» — doi is_null.
- Без подразделения считаются публикации всего ИТМО.
- Доля: metric "ratio" и "ratio_filters" — условия числителя, например [{"field": "type", "op": "eq", "value":
  "preprint"}].
- «В среднем в год» — metric "avg_per_year"; «в среднем на сотрудника» — "avg_per_person".
- «По годам» — group_by "year", «по кварталам» — "quarter", «по подразделениям» — "dept", «по типам» — "type".

Шаблон 3 (соавторство), target "persons" (соавторы или люди подразделения) или "publications" (совместные
публикации), metric "list", "count" или "exists":
- Люди и подразделения — в entities (роли — см. выше).
- "coauthor_is_itmo": false — если спрашивают про внешних соавторов; true — только про соавторов из ИТМО.
- "rank_by": "distinct_coauthors" (число различных соавторов) или "joint_publications" (число совместных
  публикаций), только вместе с order. По умолчанию для соавторов одного человека считаются совместные публикации,
  для рейтинга людей подразделения — различные соавторы; пиши ключ, только если вопрос требует другого.
- «С кем у X больше всего публикаций» — target "persons", metric "list", order "desc", limit 1.
- Для периода — filter или last_n_years.
- Не подходит ("none"): глобальные пары без якоря, внешние организации, средняя численность коллектива, цепочки
  соавторства, «ни разу не публиковался», четыре и более человек, три и более подразделения.

Шаблон 4 (репозитории), target "persons" (кто контрибьютил, кто владеет) или "repositories":
- Поля filters: license (SPDX: "MIT", "Apache-2.0"), stars_num, forks_num, contributors_count (числа),
  archived, is_fork, has_readme (true или false), language, last_updated, owner_type ("user" или
  "organization"), role (роль в репозитории, например "maintainer").
- last_updated: «не обновлялся больше N лет» — {"field": "last_updated", "op": "lt", "value": {"years_ago": N}};
  «обновлён за последний год» — op "gt", years_ago 1; конкретная дата — строка "YYYY-MM-DD".
- «Есть лицензия» — license not_null; «нет лицензии» — license is_null.
- Подразделение (репозитории, разработанные подразделением) и человек (куда внёс вклад) — в entities.
- Не подходит ("none"): GitHub-аккаунт владельца как результат, число уникальных лицензий, сравнение «с лицензией
  против без».

Шаблон 5 (цепочки), target "persons", "pattern" одно из:
- "author_contributor" — автор публикации и контрибьютор её репозитория;
- "author_not_contributor" — автор публикации, у которой есть репозиторий, но сам он в нём не контрибьютор;
- "author_with_repo" — автор публикации, у которой есть репозиторий с заданными свойствами (условия на
  репозиторий — filters с полями шаблона 4).
Подразделение — в entities. Любая другая цепочка — "none".

Шаблоны 6–10 (тема в core), target "persons" («кто»), "publications" («какие публикации, работы, статьи») или
"repositories" («где есть код»):
- Для "9": "require_code": true и "link": "mentions" («есть код», «есть ссылка на репозиторий») или "implements"
  (только если сказано «реализация», «официальная реализация», IMPLEMENTS).
- "repo_filters": условия на репозиторий (license, stars_num, language и т. д.) в том же формате, что filters;
  только вместе с require_code.
- Подразделение (шаблон 8) — в entities; период (шаблон 10) — в filter или last_n_years.

Примеры:
"""

PARSE_SYSTEM = _PARSE_RULES.replace("<<FIELDS>>", json.dumps(FIELDS, ensure_ascii=False)) + "\n".join(
    f"Вопрос: {question}\nОтвет: {json.dumps(answer, ensure_ascii=False)}" for question, answer in PARSE_EXAMPLES
)

def parse_question(client: OpenRouterClient, question: str) -> tuple[QueryPlan, dict[str, Any]]:
    """The plan and a trace of how it was obtained (raw answer, fallback reason)."""
    trace: dict[str, Any] = {"model": client.model}
    raw = client.chat_json(question, system_prompt=PARSE_SYSTEM)
    trace["raw"] = json.dumps(raw, ensure_ascii=False) if raw is not None else None
    try:
        if raw is None:
            raise LLMError(client.last_error or "no response")
        plan = QueryPlan.model_validate(raw)
    except (LLMError, ValidationError) as exc:
        logger.warning("question parsing fell back to a plain topic search: %s", exc)
        trace["fallback"] = str(exc)[:300]
        plan = fallback_plan(question)
    return plan, trace


ANSWER_SYSTEM = """Ты оформляешь ответ на вопрос о науке в ИТМО по готовым результатам поиска в графе.
Правила:
- Используй ТОЛЬКО факты из переданных данных. Не добавляй людей, статей, подразделений и чисел, которых там нет.
- Ссылки ставь только в формате Markdown [текст](url) и только с url из данных. Не придумывай url.
- Если данных мало или нет, честно скажи об этом.
- Пиши по-русски, кратко: 1–2 абзаца и при необходимости короткий список, не длиннее 200 слов.
Верни JSON-объект {"answer": "<текст ответа в Markdown>"}."""
# Without a cap the model once looped for almost seven minutes and stopped
# mid-string, leaving JSON that could not be parsed. 800 tokens fit the
# 200-word answer the prompt asks for with room to spare.
ANSWER_MAX_TOKENS = 800


def compose_answer(client: OpenRouterClient, question: str, sections: dict[str, Any], notes: list[str]) -> str:
    payload = {"question": question, "results": sections, "notes": notes}
    raw = client.chat_json(json.dumps(payload, ensure_ascii=False, default=str), system_prompt=ANSWER_SYSTEM,
                           max_tokens=ANSWER_MAX_TOKENS)
    if raw is None or not isinstance(raw.get("answer"), str):
        raise LLMError(client.last_error or "no answer in the model's response")
    return strip_unknown_links(raw["answer"], allowed_urls(sections))


def allowed_urls(sections: dict[str, Any]) -> set[str]:
    urls: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            urls.add(value)

    walk(sections)
    return urls


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def strip_unknown_links(text: str, allowed: set[str]) -> str:
    """A link the data did not supply is the model's invention: keep the words, drop the link."""
    return _MD_LINK.sub(lambda m: m.group(0) if m.group(2) in allowed else m.group(1), text)
