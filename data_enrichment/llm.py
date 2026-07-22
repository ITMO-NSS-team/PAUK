import json
import logging
import time

import requests

from .config import LLM_RATE_LIMIT_SLEEP, LLM_TIMEOUT, OPENROUTER_API_KEY, OPENROUTER_URL

logger = logging.getLogger(__name__)


def chat_json(model: str, messages: list[dict], timeout: int = LLM_TIMEOUT) -> dict | None:
    """POST в OpenRouter, вернуть распарсенный JSON ответа модели или None.

    None при отсутствии ключа, сетевой ошибке, 429, пустом content или невалидном JSON.
    """
    if not OPENROUTER_API_KEY:
        logger.error("OPENROUTER_API_KEY не задан в .env")
        return None
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("Ошибка запроса к OpenRouter: %s", exc)
        return None

    if response.status_code == 429:
        logger.warning("OpenRouter 429 rate limit, пауза %d сек", LLM_RATE_LIMIT_SLEEP)
        time.sleep(LLM_RATE_LIMIT_SLEEP)
        return None
    if response.status_code != 200:
        logger.warning("OpenRouter HTTP %d: %s", response.status_code, response.text[:200])
        return None

    try:
        content = response.json()["choices"][0]["message"].get("content")
        if not content or not content.strip():
            logger.warning("пустой content в ответе модели")
            return None
        return json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("не смог распарсить ответ модели: %s", exc)
        return None