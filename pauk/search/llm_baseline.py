"""The planner of the text-to-Cypher baseline: one LLM call from a question to a plan.

The plan says which way the question goes (`type`: graph, mixed or rag), what
shape the answer has, which entities to resolve and, for graph and mixed,
the read-only Cypher that answers it. The model never writes graph ids: every
entity names the Cypher parameter the resolver fills, and a mixed question
names the parameter that receives the semantic search's items.

Unlike the template planner in `llm.py`, the model writes the Cypher itself;
the evaluation compares its answers with hand-made gold Cypher, so the prompt
states the labelling conventions that gold follows. Its examples use units,
people and topics that are not in the validation bank, so the model is never
shown a gold answer.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from pauk.rag.retrieve import KEYWORDS_RULES
from pauk.sources.llm import OpenRouterClient

from .reader import WriteQueryRejected, assert_read_only

QuestionType = Literal["graph", "mixed", "rag"]
AnswerType = Literal["list", "scalar", "table"]
Target = Literal["persons", "publications", "repositories"]
EntityKind = Literal["person", "department"]

# Error class of a plan that could not be used (see the pipeline's classes 1-7).
INVALID_PLAN = 1

_PARAM = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_PARAM_NAME = re.compile(r"[a-z][a-z0-9_]*")


class PlanEntity(BaseModel):
    kind: EntityKind
    name: str = Field(min_length=1)
    role: str | None = None
    # The Cypher parameter the resolved id goes into.
    param: str

    @model_validator(mode="after")
    def _param_is_a_name(self) -> PlanEntity:
        if not _PARAM_NAME.fullmatch(self.param):
            raise ValueError(f"entity param {self.param!r} is not a parameter name")
        return self


class RagRequest(BaseModel):
    targets: list[Target] = Field(min_length=1)
    keywords: str = Field(min_length=1)


class Plan(BaseModel):
    type: QuestionType
    answer_type: AnswerType
    entities: list[PlanEntity] = Field(default_factory=list)
    rag: RagRequest | None = None
    rag_param: Target | None = None
    cypher: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Plan:
        if self.type == "rag":
            if self.rag is None:
                raise ValueError("a rag question needs `rag`")
            if self.cypher:
                raise ValueError("a rag question has no Cypher")
            return self
        if not self.cypher or not self.cypher.strip():
            raise ValueError(f"a {self.type} question needs `cypher`")
        try:
            assert_read_only(self.cypher)
        except WriteQueryRejected as exc:
            raise ValueError(str(exc)) from exc
        if self.type == "mixed":
            if self.rag is None or self.rag_param is None:
                raise ValueError("a mixed question needs `rag` and `rag_param`")
        elif self.rag_param is not None:
            raise ValueError("only a mixed question has `rag_param`")
        params = [entity.param for entity in self.entities]
        if len(set(params)) != len(params):
            raise ValueError(f"entity params repeat: {params}")
        declared = set(params) | ({self.rag_param} if self.rag_param else set())
        used = set(_PARAM.findall(self.cypher))
        if used - declared:
            raise ValueError(f"Cypher uses undeclared parameters {sorted(used - declared)}")
        if self.rag_param and self.rag_param not in used:
            raise ValueError(f"Cypher does not use ${self.rag_param}")
        return self


# --- the prompt ------------------------------------------------------------------

_SCHEMA = """ГРАФ (Neo4j). Узлы и свойства, которые встречаются в вопросах:
- Person: id, is_itmo (сотрудник ИТМО; у внешних соавторов false или нет), name_ru, name_en, name_raw (имя как
  в OpenAlex), surname_ru, first_name_ru, other_names, degree, github (логин), google_scholar, linkedin, orcid,
  homepage, affiliations. Пустое поле — свойства нет или оно "".
  degree хранится сокращением: "к.т.н.", "к.ф.-м.н.", "к.э.н.", ... (кандидаты — всё, что начинается с "к.",
  и "PhD"), "д.т.н.", "д.ф.-м.н.", ... (доктора — начинается с "д."). Нет степени — degree пуст.
  affiliations — JSON-строка вида [{"name": "Moscow Institute of Physics and Technology", "ror": "...",
  "years": [2021, 2022]}, ...]: организации из OpenAlex за все годы.
- Publication: id, title, abstract, year (число), publication_date, type, doi, journal, funding, pdf_urls,
  fields (области знаний), has_code (найдена ссылка на код).
  type: "article", "conference-paper", "preprint", "book-chapter", "dataset", "review", "erratum", "other", ...
  funding — JSON-строка, без финансирования "[]".
- Department: id, name_ru, name_en, name_variants, kind (megafaculty, faculty, institute, center, lab, ...),
  parent_id (id родительского подразделения) или organization_id (для подразделения верхнего уровня).
- Organization: id ("itmo-university" — корень иерархии подразделений), name_en, ror_id.
- Repository: id, name, url, description, topics, stars_num, forks_num, license (SPDX: "MIT", "Apache-2.0",
  "NOASSERTION", ...), language, last_updated (строка "YYYY-MM-DD"), has_readme, archived, is_fork,
  contributors (список логинов GitHub всех контрибьюторов).
- GitHubProfile: login, name, company, type ("user" или "organization").
- LinkCandidate: url, host — ссылка на код из текста публикации, которую не удалось распознать как репозиторий.
Связи (направление важно, в обратную сторону связи не существуют):
- (Person)-[:AUTHORED]->(Publication)              — свойства: position (номер в списке авторов),
  is_corresponding, affiliation (строка аффилиации в этой работе)
- (Person)-[:BELONGS_TO]->(Department)          — сотрудник подразделения (только у сотрудников ИТМО)
- (Person)-[:CONTRIBUTED_TO]->(Repository)        — свойство role: "owner" или "contributor"
- (Publication)-[:PRODUCED_BY]->(Department)     — публикация подразделения
- (Publication)-[:MENTIONS_LINK]->(Repository|LinkCandidate) — ссылка на код в тексте; свойства is_relevant,
  availability ("available", "not_found", "unchecked")
- (Repository)-[:IMPLEMENTS]->(Publication)       — код публикации
- (Repository)-[:OWNED_BY]->(GitHubProfile)       — владелец репозитория
- (Department)-[:PART_OF]->(Department|Organization) — иерархия подразделений
Других связей нет: между Person и GitHubProfile, между Department и Repository связи нет.
В Neo4j нет APOC. Связей DEVELOPED_BY в графе нет. Служебные свойства (rag_*, created_at, updated_at,
merged_ids) в запросах не используй."""

_RULES = """ПРАВИЛА (так размечены эталонные ответы)
- Подразделение — только его собственные связи: сотрудники по BELONGS_TO к нему, публикации по PRODUCED_BY к
  нему. Вложенные подразделения (PART_OF*) учитывай, только если в вопросе сказано «среди всех его
  подразделений» (или «во всех подразделениях»). «Каждое подразделение X» — подразделения, у которых
  PART_OF к X, каждое со своими собственными связями.
- Репозитории подразделения — те, что реализуют (IMPLEMENTS) публикации подразделения.
- «Статьи» — публикации с type "article" или "conference-paper". «Публикации», «работы» — все типы.
- «В журналах» — type "article".
- Репозиторий, связанный с ИТМО — в него вносил вклад сотрудник ИТМО (CONTRIBUTED_TO) или он реализует какую-либо
  публикацию. "NOASSERTION" — такое же значение лицензии, как остальные.
- Аккаунт GitHub принадлежит сотруднику ИТМО, если его login совпадает (без учёта регистра) с github
  какого-либо Person с is_itmo = true.
- Внешние организации берутся из affiliations внешних соавторов (is_itmo не true), за все годы; ITMO University
  не внешняя. Названия вырезай из JSON-строки через split: [s IN split(x.affiliations, '"name": "')[1..] |
  split(s, '"')[0]].
- Внешний соавтор — Person с is_itmo не true; сотрудник, «из ИТМО» — is_itmo = true.
- Годы включительно: «с 2022» — year >= 2022, «до 2020» — year <= 2019, «с 2020 по 2023» — 2020..2023.
  «За последние N лет» — N календарных лет, считая текущий год <<YEAR>>.
- «Самый», «больше всего», «наименьший» — верни всех, кто делит первое место.
- «Топ-N» — ровно N строк по убыванию.
- Если по вопросу в графе ничего нет, запрос вернёт пустой результат — это правильный ответ, не меняй запрос.
- В ответе-списке возвращай id узлов (p.id, w.id, r.id, d.id) или название организации, по одному в строке.
  В скаляре — одно значение. В таблице — колонки по смыслу вопроса: сначала ключ (подразделение d.id, год,
  организация), затем число."""

_TYPES = """ТИП ВОПРОСА (поле type)
- "graph": ответ дают связи и атрибуты графа, темы (предметной области) в вопросе нет.
- "rag": в вопросе только тема — кто ей занимается, какие публикации, где код. Запрос к графу не нужен: ответ даёт
  семантический поиск. cypher — null.
- "mixed": тема и условие на граф (подразделение, годы, наличие кода, звёзды, число людей ...). Тему находит
  семантический поиск, а условие проверяет твой Cypher: найденные поиском id код передаст в параметр,
  названный в rag_param ($publications, $persons или $repositories). В вопросах про людей по теме передавай
  публикации ($publications), а люди — авторы этих публикаций из ИТМО."""

_FIELDS = """ПОЛЯ ОТВЕТА — верни ТОЛЬКО JSON-объект:
- "type": "graph" | "mixed" | "rag".
- "answer_type": "list" (кто, какие — перечисление), "scalar" (сколько, одно значение) или "table" (по годам, по
  подразделениям, пары, топ с числами).
- "entities": конкретные люди и подразделения, НАЗВАННЫЕ в вопросе по имени: [{"kind": "person"|"department",
  "name": "...", "role": null, "param": "..."}]. Имя человека — как в вопросе («Бухановский А. В.»); подразделение —
  в именительном падеже, аббревиатуры как в вопросе. "param" — имя параметра Cypher, в который код подставит id
  сущности (department_id, person_id; для двух — a_id и b_id). id сам не пиши. Пусто, если никто не назван.
- "rag": для "rag" и "mixed" — {"targets": [...], "keywords": "..."}; иначе null.
  targets — что искать: "persons" (вопрос «кто»: люди и их публикации — ["persons", "publications"]),
  "publications" (какие публикации, работы), "repositories" (где код).
  keywords: <<KEYWORDS>>
- "rag_param": для "mixed" — "publications", "persons" или "repositories"; иначе null.
- "cypher": для "graph" и "mixed" — один запрос только на чтение (MATCH/WHERE/WITH/RETURN, без CREATE, MERGE,
  SET, DELETE), параметры — только из entities[].param и rag_param; для "rag" — null."""

EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    ("Сколько сотрудников факультета фотоники указали ORCID?",
     {"type": "graph", "answer_type": "scalar",
      "entities": [{"kind": "department", "name": "Факультет фотоники", "role": None, "param": "department_id"}],
      "rag": None, "rag_param": None,
      "cypher": "MATCH (p:Person)-[:BELONGS_TO]->(:Department {id: $department_id}) "
                "WHERE p.orcid IS NOT NULL AND p.orcid <> '' RETURN count(DISTINCT p) AS value"}),
    ("Сколько публикаций выходило по годам с 2021 по 2023 у факультета фотоники среди всех его подразделений?",
     {"type": "graph", "answer_type": "table",
      "entities": [{"kind": "department", "name": "Факультет фотоники", "role": None, "param": "department_id"}],
      "rag": None, "rag_param": None,
      "cypher": "MATCH (w:Publication)-[:PRODUCED_BY]->(:Department)-[:PART_OF*0..]->"
                "(:Department {id: $department_id}) WHERE w.year >= 2021 AND w.year <= 2023 "
                "RETURN w.year AS year, count(DISTINCT w) AS count ORDER BY year"}),
    ("Сколько публикаций у каждого подразделения факультета фотоники?",
     {"type": "graph", "answer_type": "table",
      "entities": [{"kind": "department", "name": "Факультет фотоники", "role": None, "param": "department_id"}],
      "rag": None, "rag_param": None,
      "cypher": "MATCH (u:Department)-[:PART_OF]->(:Department {id: $department_id}) "
                "MATCH (w:Publication)-[:PRODUCED_BY]->(u) "
                "RETURN u.id AS department, count(DISTINCT w) AS count ORDER BY count DESC, department"}),
    ("Сколько совместных статей у Лебедева А. В. и Орловой Н. С.?",
     {"type": "graph", "answer_type": "scalar",
      "entities": [{"kind": "person", "name": "Лебедев А. В.", "role": None, "param": "a_id"},
                   {"kind": "person", "name": "Орлова Н. С.", "role": None, "param": "b_id"}],
      "rag": None, "rag_param": None,
      "cypher": "MATCH (:Person {id: $a_id})-[:AUTHORED]->(w:Publication)<-[:AUTHORED]-(:Person {id: $b_id}) "
                "WHERE w.type IN ['article', 'conference-paper'] RETURN count(DISTINCT w) AS value"}),
    ("Какие репозитории факультета фотоники распространяются по лицензии GPL-3.0?",
     {"type": "graph", "answer_type": "list",
      "entities": [{"kind": "department", "name": "Факультет фотоники", "role": None, "param": "department_id"}],
      "rag": None, "rag_param": None,
      "cypher": "MATCH (r:Repository)-[:IMPLEMENTS]->(:Publication)-[:PRODUCED_BY]->"
                "(:Department {id: $department_id}) WHERE r.license = 'GPL-3.0' RETURN DISTINCT r.id AS id ORDER BY id"}),
    ("У кого из факультета фотоники больше всего соавторов не из ИТМО?",
     {"type": "graph", "answer_type": "list",
      "entities": [{"kind": "department", "name": "Факультет фотоники", "role": None, "param": "department_id"}],
      "rag": None, "rag_param": None,
      "cypher": "MATCH (p:Person)-[:BELONGS_TO]->(:Department {id: $department_id}) WITH DISTINCT p "
                "MATCH (p)-[:AUTHORED]->(:Publication)<-[:AUTHORED]-(x:Person) WHERE NOT coalesce(x.is_itmo, false) "
                "WITH p, count(DISTINCT x) AS n WITH collect({id: p.id, n: n}) AS rows, max(n) AS best "
                "UNWIND rows AS row WITH row, best WHERE row.n = best RETURN row.id AS id ORDER BY id"}),
    ("Кто занимается метаматериалами?",
     {"type": "rag", "answer_type": "list", "entities": [],
      "rag": {"targets": ["persons", "publications"], "keywords": "metamaterials"},
      "rag_param": None, "cypher": None}),
    ("Где есть код для обработки спутниковых снимков?",
     {"type": "rag", "answer_type": "list", "entities": [],
      "rag": {"targets": ["repositories"], "keywords": "satellite image processing remote sensing"},
      "rag_param": None, "cypher": None}),
    ("Кто из факультета фотоники публиковал статьи про фотонные кристаллы в 2022 году?",
     {"type": "mixed", "answer_type": "list",
      "entities": [{"kind": "department", "name": "Факультет фотоники", "role": None, "param": "department_id"}],
      "rag": {"targets": ["persons", "publications"], "keywords": "photonic crystals"},
      "rag_param": "publications",
      "cypher": "MATCH (p:Person {is_itmo: true})-[:AUTHORED]->(w:Publication) WHERE w.id IN $publications "
                "AND w.year = 2022 AND w.type IN ['article', 'conference-paper'] "
                "MATCH (p)-[:BELONGS_TO]->(:Department {id: $department_id}) RETURN DISTINCT p.id AS id ORDER BY id"}),
]


def planner_system(year: int) -> str:
    """The system prompt; `year` is the current year of the graph snapshot ("за последние N лет")."""
    examples = "\n".join(f"Вопрос: {question}\nОтвет: {json.dumps(plan, ensure_ascii=False)}"
                         for question, plan in EXAMPLES)
    return "\n\n".join([
        "Ты разбираешь вопросы к графу научных данных Университета ИТМО и пишешь к ним запросы Cypher.",
        _SCHEMA,
        _RULES.replace("<<YEAR>>", str(year)),
        _TYPES,
        _FIELDS.replace("<<KEYWORDS>>", " ".join(KEYWORDS_RULES.split())),
        "Примеры:\n" + examples,
    ])


# --- one call ----------------------------------------------------------------------

# A reply that is not a usable plan is asked for once more: a second miss is error class 1.
ATTEMPTS = 2
# A plan takes 60-300 tokens. Without a cap a model caught in a loop writes on to its own
# limit - 16k tokens and 11 minutes on one question - and the HTTP timeout never fires,
# since it counts the silence between bytes; cut short, the reply fails to parse and is asked again.
MAX_TOKENS = 1024


@dataclass
class PlanResult:
    plan: Plan | None
    error: str | None = None
    # 1 (invalid plan) or None; the other classes are set further down the pipeline.
    error_class: int | None = None
    # The raw reply of every attempt, in order.
    raw: list[Any] = field(default_factory=list)
    # Summed over the attempts.
    usage: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    attempts: int = 0


def parse_plan(raw: Any) -> Plan:
    """The validated plan of a model reply; ValueError when it is not one."""
    if not isinstance(raw, dict):
        raise ValueError(f"reply is {type(raw).__name__}, not a JSON object")
    try:
        return Plan.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def prompt_id(year: int) -> str:
    """Names the prompt a plan was made with: an edited prompt must not reuse old plans."""
    return hashlib.sha1(planner_system(year).encode()).hexdigest()[:8]


def plan_question(client: OpenRouterClient, question: str, year: int | None = None) -> PlanResult:
    """The planner on one question, asked again once if the reply is no usable plan.

    A reply that is missing, not JSON or not a valid plan (an apology, an error
    text, fields left out) gets one more try; if that fails too, the result
    carries error class 1 - never an exception.
    """
    system = planner_system(year or date.today().year)
    result = PlanResult(None)
    start = time.perf_counter()
    for _ in range(ATTEMPTS):
        result.attempts += 1
        raw = client.chat_json(question, system_prompt=system, max_tokens=MAX_TOKENS)
        for key, value in (client.last_usage or {}).items():
            if isinstance(value, int):
                result.usage[key] = result.usage.get(key, 0) + value
        result.raw.append(raw)
        if raw is None:
            result.error = client.last_error or "no response"
            continue
        try:
            result.plan = parse_plan(raw)
        except ValueError as exc:
            result.error = str(exc)[:500]
            continue
        result.error = None
        break
    result.error_class = None if result.plan else INVALID_PLAN
    result.seconds = round(time.perf_counter() - start, 3)
    return result
