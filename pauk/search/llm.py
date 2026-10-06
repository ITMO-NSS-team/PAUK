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


PARSE_EXAMPLES = [
    ("Кто в ИТМО занимается диффузионными моделями?",
     {"entities": [], "core": ["диффузионные модели", "diffusion models", "denoising diffusion generative models"],
      "expected_values": ["persons", "departments", "publications", "repositories"], "fields": [],
      "filter": {"only_itmo": True, "year_from": None, "year_to": None}, "graph_type": "topic_subgraph"}),
    ("Сколько публикаций у Иванова Ивана с 2024 года?",
     {"entities": [{"kind": "person", "name": "Иванов Иван"}], "core": [], "expected_values": ["counts",
      "publications"], "fields": [], "filter": {"only_itmo": True, "year_from": 2024, "year_to": None},
      "graph_type": "person_profile"}),
]

PARSE_SYSTEM = f"""Ты разбираешь вопросы к графу научных данных Университета ИТМО.
В графе есть: публикации (название, аннотация, год), авторы (сотрудники ИТМО и внешние соавторы),
подразделения (факультеты, институты, лаборатории, иерархия), GitHub-репозитории и их контрибьюторы.

Верни ТОЛЬКО JSON-объект с полями:
- "entities": конкретные объекты, НАЗВАННЫЕ в вопросе по имени: [{{"kind": "person"|"department", "name": "..."}}].
  Пусто, если никто не назван.
- "core": ключевые словосочетания ТЕМЫ для семантического поиска - для понимания контекста необходимо. Пиши и по-русски, и по-английски
  (тексты публикаций на английском), 1–4 фразы. Пусто, если темы нет (например, «сколько статей у X»).
  Только то, что отличает тему: названия методов, моделей, задач, областей. Не добавляй отдельно общие слова
  вроде «архитектуры», «методы», «подходы», «модели», «системы», «исследования», «применение» — они совпадают
  с чем угодно. Названия моделей и методов (ResNet, RT-DETR, BERT) пиши как есть, без перевода.
- "expected_values": какие разделы нужны в ответе, из
  ["persons", "departments", "publications", "repositories", "counts"].
  "counts" — для вопросов «сколько», статистики. Не включай разделы, которые явно исключены («без репозиториев»).
- "fields": какие поля показать, если пользователь явно ограничил; иначе []. Допустимые: {json.dumps(FIELDS, ensure_ascii=False)}
- "filter": {{"only_itmo": true|false, "year_from": число|null, "year_to": число|null}}.
  only_itmo=true по умолчанию; false, только если явно просят внешних соавторов.
- "graph_type": "topic_subgraph" (поиск по теме), "person_profile" (вопрос про конкретного автора),
  "department_profile" (вопрос про конкретное подразделение).

Примеры:
""" + "\n".join(
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
