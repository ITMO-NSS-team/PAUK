import ast
import json
import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

from config import (
    DB_PATH,
    PERSONS_RU_CHUNK_SIZE as CHUNK_SIZE,
    PERSONS_RU_MODEL as MODEL,
    PERSONS_RU_SLEEP_BETWEEN_CHUNKS as SLEEP_BETWEEN_CHUNKS,
)
from llm import chat_json

SYSTEM_PROMPT = """\
Ты транслитерируешь имена сотрудников ИТМО с английского на русский.

Тебе дают пачку персон: id, name_en и варианты написания. Для КАЖДОЙ верни
русские ФИО:
  - surname_ru   — фамилия,
  - first_name_ru — имя,
  - second_name_ru — отчество (только если оно явно видно из имени; иначе "").
Не выдумывай отчество. Если имя/фамилию транслитерировать однозначно нельзя
— оставь соответствующее поле пустым.

Верни СТРОГО валидный JSON без markdown, объект с ключом "persons" — массив
ровно по числу входных персон, порядок сохраняй:
{"persons":[{"id":"...","surname_ru":"...","first_name_ru":"...","second_name_ru":""}]}
"""


def parse_variants(raw: str | None) -> list[str]:
    """name_variants хранится как JSON или python-list-литерал — парсим оба."""
    if not raw:
        return []
    for loader in (json.loads, ast.literal_eval):
        try:
            v = loader(raw)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        except (ValueError, SyntaxError, TypeError):
            continue
    return []


def call_llm(chunk: list[tuple[str, str, str]]) -> dict | None:
    """Отправляет пачку персон (id + name_en + варианты) в LLM."""
    lines = []
    for pid, name_en, variants_raw in chunk:
        variants = parse_variants(variants_raw)
        variants_str = "; ".join(variants) if variants else "—"
        lines.append(f"id: {pid}   name_en: {name_en or '-'}   variants: {variants_str}")
    user_prompt = (
        f"ПАЧКА ПЕРСОН (всего {len(chunk)}):\n" + "\n".join(lines) +
        f"\n\nВерни JSON с массивом ровно из {len(chunk)} объектов, порядок сохраняй."
    )
    return chat_json(
        MODEL,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )


def fetch_persons_without_ru(conn: sqlite3.Connection) -> list[tuple]:
    """(id, name_en, name_variants) для людей без проставленного surname_ru."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name_en, name_variants
        FROM persons_itmo
        WHERE surname_ru IS NULL
        ORDER BY id
        """
    )
    return cur.fetchall()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    try:
        persons = fetch_persons_without_ru(conn)
        if not persons:
            logger.info("Все сотрудники уже обогащены русскими ФИО.")
            return

        chunks = [persons[i : i + CHUNK_SIZE] for i in range(0, len(persons), CHUNK_SIZE)]
        logger.info("Обогащаю %d человек через %s (%d чанков по %d)", len(persons), MODEL, len(chunks), CHUNK_SIZE)
        stats = {"filled": 0, "empty": 0, "failed_chunks": 0}

        for idx, chunk in enumerate(chunks, 1):
            logger.info("[чанк %d/%d] %d чел.", idx, len(chunks), len(chunk))
            result = call_llm(chunk)
            if result is None:
                stats["failed_chunks"] += 1
                time.sleep(SLEEP_BETWEEN_CHUNKS)
                continue

            for res in result.get("persons", []):
                pid = res.get("id")
                if not pid:
                    continue
                surname = (res.get("surname_ru") or "").strip()
                first = (res.get("first_name_ru") or "").strip()
                second = (res.get("second_name_ru") or "").strip()
                cur.execute(
                    "UPDATE persons_itmo SET surname_ru=?, first_name_ru=?, second_name_ru=? WHERE id=?",
                    (surname, first, second, pid),
                )
                stats["filled" if surname else "empty"] += 1
            conn.commit()
            time.sleep(SLEEP_BETWEEN_CHUNKS)

        logger.info(
            "Заполнено ФИО: %d, пустых: %d, чанков с ошибкой: %d",
            stats["filled"], stats["empty"], stats["failed_chunks"],
        )
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    main()
