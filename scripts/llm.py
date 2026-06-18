import json
import time

import requests
from config import DOWNLOAD_TIMEOUT, OPENROUTER_API_KEY, OPENROUTER_URL


def chat_json(model: str, messages: list[dict], timeout: int = DOWNLOAD_TIMEOUT) -> dict | None:
    """POST в OpenRouter, вернуть распарсенный JSON ответа модели или None.

    None при: нет ключа, сетевой ошибке, 429/нештатном статусе, пустом
    content (reasoning-модели иногда так делают) или невалидном JSON.
    """
    if not OPENROUTER_API_KEY:
        print("  OPENROUTER_API_KEY не задан в .env")
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
        print(f" Ошибка запроса к OpenRouter: {exc}")
        return None

    if response.status_code == 429:
        print(" OpenRouter 429 (rate limit), пауза 30 секунд")
        time.sleep(30)
        return None
    if response.status_code != 200:
        print(f" OpenRouter HTTP {response.status_code}: {response.text[:200]}")
        return None

    try:
        content = response.json()["choices"][0]["message"].get("content")
        if not content or not content.strip():
            print("  пустой content в ответе модели")
            return None
        return json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
        print(f"  не смог распарсить ответ модели: {exc}")
        return None
