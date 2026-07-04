import json
import logging
import re
import sqlite3
import time
import uuid

logger = logging.getLogger(__name__)

from config import (
    DB_PATH,
    DEPT_CHUNK_SIZE,
    DEPT_MODEL,
    DEPT_SLEEP_BETWEEN_CHUNKS,
    DEPT_TIMEOUT,
)
from llm import chat_json

NO_DEPT_SENTINEL = "-"

SYSTEM_PROMPT = """\
Ты — эксперт по организационной структуре университета ИТМО. Твоя задача —
ВРУЧНУЮ найти соответствия между аффилиациями людей и списком департаментов,
никаких автоматических алгоритмов.

Ты получишь:
1. ПОЛНЫЙ список существующих департаментов ИТМО в формате:
   - id: <идентификатор>
   - name_en: <официальное английское название>
   - variants: [<вариант1>, <вариант2>, ...]
2. ПАЧКУ персон, для каждой дано сырое поле affiliation (несколько строк,
   разделённых " \\n ").

ДЕЙСТВИЯ:
1. Просмотри все аффилиации в чанке. Выдели упоминания подразделений ИТМО.
   - Голый университет без подразделения (ITMO University, ITMO, Университет
     ИТМО) — НЕ извлекай.
   - Если в одной строке перечислено несколько подразделений (через запятую,
     ';' или "and") — извлеки КАЖДОЕ отдельно.
   - Нормализуй: убери хвостовое "ITMO University", лишние запятые/кавычки,
     приведи к Title Case, схлопни двойные пробелы.
2. Для каждого извлечённого названия вручную сравни его с каждым существующим
   департаментом (name_en и все variants):
   - Незначительные различия (кавычки, регистр, предлоги, Diagnostic/Diagnostics,
     Center/Centre, Laboratory/Lab) — ЭТО ОДНО И ТО ЖЕ.
   - Перестановка слов (Institute of AI ↔ AI Institute) — ОДНО И ТО ЖЕ.
   - Если одно название — лишь часть другого, более длинного — это РАЗНЫЕ
     подразделения (если сомневаешься — считай разными).
   - Явные опечатки одной и той же лаборатории — совпадение.
   - Нашёл → matched=true, existing_name_en = каноничное имя из списка.
     Если написание слегка отличается — add_variant_to_existing=true.
   - Не нашёл → matched=false, is_new=true, предложи new_name_en (и, если
     можешь, new_name_ru). Не плоди дубликаты внутри чанка: одинаковые по
     смыслу новые названия должны иметь идентичный new_name_en.
3. Для каждой персоны собери массив подразделений (без повторов).

Верни СТРОГО валидный JSON по схеме, без markdown-обёрток:
{
  "persons": [
    {
      "person_id": "...",
      "departments": [
        {
          "extracted_name": "...",
          "matched": true,
          "existing_name_en": "...",
          "is_new": false,
          "new_name_en": "",
          "new_name_ru": "",
          "add_variant_to_existing": false
        }
      ]
    }
  ]
}
"""


# --- Работа с departments -----------------------------------------------


def normalize_name(s: str) -> str:
    """Убирает кавычки, схлопывает пробелы, приводит к нижнему регистру."""
    if not s:
        return ""
    s = re.sub(r'["“”‘’«»]', "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def load_departments(cur: sqlite3.Cursor) -> dict[str, dict]:
    """Все департаменты как {id: {'name_en': str, 'name_variants': list}}."""
    cur.execute("SELECT id, name_en, name_variants FROM departments")
    depts: dict[str, dict] = {}
    for dept_id, name_en, variants_raw in cur.fetchall():
        try:
            variants = json.loads(variants_raw) if variants_raw else []
            if not isinstance(variants, list):
                variants = []
        except (TypeError, ValueError):
            variants = []
        depts[dept_id] = {"name_en": name_en, "name_variants": variants}
    return depts


def format_departments_for_prompt(departments: dict[str, dict]) -> str:
    lines = []
    for dept_id, info in departments.items():
        variants = info["name_variants"]
        variants_str = ", ".join(f'"{v}"' for v in variants) if variants else "—"
        lines.append(
            f"- id: {dept_id}\n  name_en: {info['name_en']}\n  variants: [{variants_str}]"
        )
    return "\n".join(lines)


def find_dept_id_by_name(name: str, departments: dict[str, dict]) -> str | None:
    """Ищет id департамента по точному (после нормализации) совпадению."""
    norm = normalize_name(name)
    if not norm:
        return None
    for dept_id, info in departments.items():
        if normalize_name(info["name_en"]) == norm:
            return dept_id
        if any(normalize_name(v) == norm for v in info["name_variants"]):
            return dept_id
    return None


def generate_dept_id() -> str:
    return f"dept_{uuid.uuid4().hex[:12]}"


def add_variant(
    cur: sqlite3.Cursor, dept_id: str, variant: str, departments: dict[str, dict]
) -> None:
    """Дописывает новый вариант написания департаменту, если его ещё нет."""
    info = departments.get(dept_id)
    if not info or not variant:
        return
    norm = normalize_name(variant)
    if norm == normalize_name(info["name_en"]):
        return
    if norm in {normalize_name(v) for v in info["name_variants"]}:
        return
    info["name_variants"].append(variant)
    cur.execute(
        "UPDATE departments SET name_variants = ? WHERE id = ?",
        (json.dumps(info["name_variants"], ensure_ascii=False), dept_id),
    )


def create_department(
    cur: sqlite3.Cursor,
    name_en: str,
    name_ru: str | None,
    departments: dict[str, dict],
) -> str | None:
    """Создаёт новый департамент (или возвращает существующий по имени)."""
    existing = find_dept_id_by_name(name_en, departments)
    if existing:
        return existing
    dept_id = generate_dept_id()
    try:
        cur.execute(
            "INSERT INTO departments (id, name_en, name_ru) VALUES (?, ?, ?)",
            (dept_id, name_en, name_ru or None),
        )
    except sqlite3.IntegrityError:
        cur.execute("SELECT id FROM departments WHERE name_en = ?", (name_en,))
        row = cur.fetchone()
        if not row:
            return None
        dept_id = row[0]
    departments[dept_id] = {"name_en": name_en, "name_variants": []}
    return dept_id


# --- LLM -----------------------------------------------------------------


def call_llm(chunk: list[tuple[str, str]], departments: dict[str, dict]) -> dict | None:
    """Отправляет чанк персон + список департаментов в LLM."""
    depts_text = format_departments_for_prompt(departments)
    persons_block = "\n---\n".join(
        f"person_id: {pid}\naffiliation: {aff or ''}" for pid, aff in chunk
    )
    user_prompt = (
        f"СПИСОК СУЩЕСТВУЮЩИХ ДЕПАРТАМЕНТОВ:\n{depts_text}\n\n"
        f"===\nДАННЫЕ ПЕРСОН (всего {len(chunk)}):\n{persons_block}\n\n"
        "Вручную сопоставь все подразделения и верни JSON."
    )
    return chat_json(
        DEPT_MODEL,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        timeout=DEPT_TIMEOUT,
    )


def resolve_person_departments(
    person_res: dict, cur: sqlite3.Cursor, departments: dict[str, dict]
) -> list[str]:
    """Превращает departments одного человека из ответа LLM в список dept_id."""
    dept_ids: list[str] = []
    for dep in person_res.get("departments", []):
        if dep.get("is_new"):
            name = (dep.get("new_name_en") or dep.get("extracted_name") or "").strip()
            if not name:
                continue
            did = create_department(cur, name, dep.get("new_name_ru"), departments)
            if did:
                dept_ids.append(did)
            continue

        name = (dep.get("existing_name_en") or "").strip()
        did = find_dept_id_by_name(name, departments) if name else None
        if did is None:
            fallback = name or (dep.get("extracted_name") or "").strip()
            if fallback:
                did = create_department(cur, fallback, None, departments)
        if did:
            dept_ids.append(did)
            if dep.get("add_variant_to_existing"):
                add_variant(cur, did, (dep.get("extracted_name") or "").strip(), departments)

    return list(dict.fromkeys(dept_ids))


# --- БД -------------------------------------------------------------------


def fetch_persons_without_department(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(id, affiliation) для людей с аффилиацией, но без проставленного департамента."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, affiliation
        FROM persons_itmo
        WHERE affiliation IS NOT NULL AND affiliation != ''
          AND (department IS NULL OR department = '')
        ORDER BY id
        """
    )
    return cur.fetchall()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    try:
        persons = fetch_persons_without_department(conn)
        if not persons:
            logger.info("Все люди с аффилиацией уже размечены департаментами.")
            return

        chunks = [persons[i : i + DEPT_CHUNK_SIZE] for i in range(0, len(persons), DEPT_CHUNK_SIZE)]
        logger.info(
            "Размечаю %d человек через %s (%d чанков по %d)",
            len(persons), DEPT_MODEL, len(chunks), DEPT_CHUNK_SIZE,
        )
        stats = {"updated": 0, "no_dept": 0, "failed_chunks": 0, "new_depts_start": 0}
        stats["new_depts_start"] = cur.execute(
            "SELECT COUNT(*) FROM departments"
        ).fetchone()[0]

        for idx, chunk in enumerate(chunks, 1):
            departments = load_departments(cur)
            logger.info("[чанк %d/%d] %d чел., депов в базе: %d", idx, len(chunks), len(chunk), len(departments))
            result = call_llm(chunk, departments)
            if result is None:
                stats["failed_chunks"] += 1
                time.sleep(DEPT_SLEEP_BETWEEN_CHUNKS)
                continue

            for person_res in result.get("persons", []):
                pid = person_res.get("person_id")
                if not pid:
                    continue
                dept_ids = resolve_person_departments(person_res, cur, departments)
                if dept_ids:
                    dept_str = "; ".join(dept_ids)
                    stats["updated"] += 1
                else:
                    dept_str = NO_DEPT_SENTINEL
                    stats["no_dept"] += 1
                cur.execute(
                    "UPDATE persons_itmo SET department = ? WHERE id = ?",
                    (dept_str, pid),
                )
            conn.commit()
            time.sleep(DEPT_SLEEP_BETWEEN_CHUNKS)

        new_depts = (
            cur.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
            - stats["new_depts_start"]
        )
        logger.info(
            "Размечено: %d, без департамента: %d, новых депов: %d, чанков с ошибкой: %d",
            stats["updated"], stats["no_dept"], new_depts, stats["failed_chunks"],
        )
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    main()
