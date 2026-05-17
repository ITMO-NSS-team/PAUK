"""Прогоняет кандидатные ссылки из repo_links через LLM и проставляет is_relevant.

Для каждой строки repo_links, у которой is_relevant IS NULL, скрипт собирает
URL + хост + контекст +-N символов из PDF + название и авторов статьи (через
JOIN с publications) и отправляет это в LLM через OpenRouter API. LLM должен
решить, ведёт ли ссылка на код/модель/датасет, выложенный самими авторами
этой статьи, или это просто упоминание чужого инструмента.

Ответ LLM сохраняется в repo_links: is_relevant (bool), llm_confidence (0..1),
llm_reason (короткое объяснение, для отладки и просмотра пограничных случаев).

Параметр --limit задаёт, сколько кандидатов взять за один
запуск.

Запускать из корня проекта:
    uv run python scripts/classify_repo_links.py --limit 50
"""

import argparse
import json
import sqlite3
import time

import requests
from config import (
    CLASSIFY_BATCH_SIZE,
    DB_PATH,
    DOWNLOAD_TIMEOUT,
    LLM_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    REQUEST_DELAY,
)

PROMPT_TEMPLATE = """Ты помогаешь анализировать научные публикации.

Публикация:
  Название: {title}
  Авторы:   {authors}

В её материалах найдена ссылка:
  URL:  {url}
  Хост: {host}
  {source_hint}

Окружающий текст:
\"\"\"
{context}
\"\"\"

Вопрос: это репозиторий/модель/датасет, который ВЫЛОЖИЛИ САМИ
АВТОРЫ этой статьи как сопроводительный материал — или это
упоминание чужого инструмента?

Признаки авторского артефакта:
  - "our code is available at", "we release", "we provide",
    "наш код доступен", "исходный код размещён".
  - Имя пользователя/организации в URL похоже на одного из
    авторов или их аффилиацию.

Признаки чужого:
  - Ссылка в библиографическом списке (References, Литература).
  - Известная чужая библиотека/модель (PyTorch, BERT, Llama,
    HuggingFace official org).
  - Формулировки "we use", "based on", "following [N]".

Ответь СТРОГО валидным JSON, без markdown-обёрток:
{{
  "is_authors_artifact": true,
  "confidence": 0.0,
  "reason": "одно короткое предложение"
}}
"""


def build_prompt(
    title: str,
    authors: str | None,
    url: str,
    host: str,
    context: str,
    page_number: int | None,
) -> str:
    """Собирает текст промпта из полей одной строки repo_links."""
    if page_number is None:
        source_hint = "Источник: абстракт из OpenAlex (контекст ограничен)."
    else:
        source_hint = f"Источник: видимый текст PDF, страница {page_number}."
    return PROMPT_TEMPLATE.format(
        title=title or "(без названия)",
        authors=authors or "(авторы не указаны)",
        url=url,
        host=host or "?",
        source_hint=source_hint,
        context=context or "(контекст пустой)",
    )


def call_llm(prompt: str) -> dict | None:
    """Отправляет промпт в OpenRouter и возвращает распарсенный JSON-ответ модели.

    При сетевой ошибке, невалидном JSON в ответе или любом нештатном статусе
    возвращает None — вызывающий код решит, что делать (обычно пропускаем
    строку и оставляем is_relevant NULL до следующего запуска).
    """
    if not OPENROUTER_API_KEY:
        print("  OPENROUTER_API_KEY не задан в .env")
        return None

    try:
        response = requests.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=DOWNLOAD_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"  ошибка запроса к OpenRouter: {exc}")
        return None

    if response.status_code == 429:
        print("  OpenRouter вернул 429 (rate limit), пауза 30 секунд")
        time.sleep(30)
        return None
    if response.status_code != 200:
        print(f"  OpenRouter HTTP {response.status_code}: {response.text[:200]}")
        return None

    try:
        envelope = response.json()
        content = envelope["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
        print(f"  не смог распарсить ответ модели: {exc}")
        return None


def fetch_unclassified(conn: sqlite3.Connection, limit: int) -> list[tuple]:
    """Возвращает кандидаты с присоединёнными title/authors из publications."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            rl.id, rl.publication_id, rl.url, rl.host, rl.context, rl.page_number,
            p.title, p.authors
        FROM repo_links rl
        JOIN publications p ON p.id = rl.publication_id
        WHERE rl.is_relevant IS NULL
        ORDER BY rl.id
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


def save_classification(
    conn: sqlite3.Connection,
    link_id: int,
    is_relevant: bool,
    confidence: float,
    reason: str,
) -> None:
    """Сохраняет результат классификации в repo_links для одной строки."""
    conn.execute(
        """
        UPDATE repo_links
        SET is_relevant = ?, llm_confidence = ?, llm_reason = ?
        WHERE id = ?
        """,
        (1 if is_relevant else 0, confidence, reason, link_id),
    )
    conn.commit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Классифицирует кандидатные ссылки в repo_links через LLM."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=CLASSIFY_BATCH_SIZE,
        help=f"Сколько кандидатов взять за один запуск (по умолчанию: {CLASSIFY_BATCH_SIZE}).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = fetch_unclassified(conn, args.limit)
        if not rows:
            print("Нет неклассифицированных ссылок в repo_links.")
            return

        print(f"Классифицирую {len(rows)} ссылок через {LLM_MODEL}")
        stats = {"yes": 0, "no": 0, "failed": 0}

        for index, row in enumerate(rows, 1):
            link_id, pub_id, url, host, context, page, title, authors = row
            print(f"[{index}/{len(rows)}] {pub_id} | {host} | {url[:70]}")

            prompt = build_prompt(title, authors, url, host, context, page)
            result = call_llm(prompt)

            if result is None:
                stats["failed"] += 1
                time.sleep(REQUEST_DELAY)
                continue

            is_relevant = bool(result.get("is_authors_artifact"))
            try:
                confidence = float(result.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            reason = str(result.get("reason") or "").strip()

            save_classification(conn, link_id, is_relevant, confidence, reason)
            verdict = "ДА" if is_relevant else "нет"
            print(f"  -> {verdict} (уверенность {confidence:.2f}) — {reason}")
            stats[("yes" if is_relevant else "no")] += 1

            time.sleep(REQUEST_DELAY)

        print()
        print(f"Распознано как авторских: {stats['yes']}")
        print(f"Распознано как чужих:     {stats['no']}")
        print(f"Не удалось обработать:    {stats['failed']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
