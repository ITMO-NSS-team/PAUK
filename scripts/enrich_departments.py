"""Департаменты ИТМО: 2-этапное сопоставление аффилиаций + перевод названий.

Режимы:
  --mode match     (по умолчанию) сопоставляет persons_itmo.affiliation с департаментами:
                   Stage 1 — точный матч по официальному каталогу (+ выученные алиасы),
                             без LLM; Stage 2 — остаток через LLM с якорем на официальные
                             русские названия. Гибрид: неофициальные единицы создаются как есть.
  --mode translate заполняет пустой name_ru неофициальных депов LLM-переводом
                   (официальные name_ru — из каталога, seed_departments; не трогаются).

Каталог (data/departments_catalog.json) — источник истины; заливается seed_departments.py
ПЕРЕД этим шагом.
"""

import argparse
import json
import logging
import re
import sqlite3
import time
import uuid

from affiliations import clean_affiliation, has_department_mention
from catalog import load_catalog, normalize as catalog_normalize, official_name_en_set
from config import (
    DB_PATH,
    DEPT_CHUNK_SIZE,
    DEPT_MODEL,
    DEPT_SLEEP_BETWEEN_CHUNKS,
    DEPT_TIMEOUT,
    DEPT_TRANSLATE_CHUNK_SIZE,
    DEPT_TRANSLATE_MODEL,
    DEPT_TRANSLATE_SLEEP_BETWEEN_CHUNKS,
)
from llm import chat_json

logger = logging.getLogger(__name__)

NO_DEPT_SENTINEL = "-"

SYSTEM_PROMPT = """\
Ты — эксперт по организационной структуре университета ИТМО. Твоя задача —
ВРУЧНУЮ сопоставить аффилиации людей со списком департаментов, никаких
автоматических алгоритмов.

Ты получишь:
1. ПОЛНЫЙ список департаментов ИТМО: id, name_en, name_ru, variants. Записи с
   name_ru — это ОФИЦИАЛЬНАЯ структура ИТМО; предпочитай матч именно с ними.
2. ПАЧКУ персон, для каждой — поле affiliation: как правило это уже извлечённый
   список подразделений (через ';'); иногда — сырой текст.

ДЕЙСТВИЯ:
1. Выдели упоминания подразделений ИТМО (если поле уже список — бери как есть).
   - Голый университет без подразделения (ITMO University, ITMO, Университет
     ИТМО) — НЕ извлекай.
   - Несколько подразделений в одной записи (через запятую, ';' или "and") —
     извлеки КАЖДОЕ отдельно.
2. Сравни каждое извлечённое название с департаментами (name_en, name_ru, variants).
   Авторы переводят названия своих подразделений на английский ПРОИЗВОЛЬНО —
   сопоставляй по смыслу с официальным русскоязычным названием из списка:
   - Незначительные различия (кавычки, регистр, предлоги, Diagnostic/Diagnostics,
     Center/Centre, Laboratory/Lab) — ЭТО ОДНО И ТО ЖЕ.
   - Перестановка слов (Institute of AI ↔ AI Institute) — ОДНО И ТО ЖЕ.
   - Если одно название — лишь часть другого, более длинного — это РАЗНЫЕ
     подразделения (если сомневаешься — считай разными).
   - Явные опечатки одной и той же лаборатории — совпадение.
   - Нашёл → matched=true, existing_name_en = каноничное name_en из списка;
     если написание слегка отличается — add_variant_to_existing=true (так система
     запоминает авторский перевод).
   - Не нашёл ни в официальных, ни среди прочих → matched=false, is_new=true,
     предложи new_name_en. Не плоди дубликаты внутри чанка.
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
    """Все департаменты как {id: {'name_en', 'name_ru', 'name_variants'}}."""
    cur.execute("SELECT id, name_en, name_ru, name_variants FROM departments")
    depts: dict[str, dict] = {}
    for dept_id, name_en, name_ru, variants_raw in cur.fetchall():
        try:
            variants = json.loads(variants_raw) if variants_raw else []
            if not isinstance(variants, list):
                variants = []
        except (TypeError, ValueError):
            variants = []
        depts[dept_id] = {"name_en": name_en, "name_ru": name_ru, "name_variants": variants}
    return depts


def format_departments_for_prompt(departments: dict[str, dict]) -> str:
    lines = []
    for dept_id, info in departments.items():
        variants = info["name_variants"]
        variants_str = ", ".join(f'"{v}"' for v in variants) if variants else "—"
        lines.append(
            f"- id: {dept_id}\n  name_en: {info['name_en']}\n"
            f"  name_ru: {info.get('name_ru') or '—'}\n  variants: [{variants_str}]"
        )
    return "\n".join(lines)


def find_dept_id_by_name(name: str, departments: dict[str, dict]) -> str | None:
    """Ищет id департамента по точному (после нормализации) совпадению name_en/variants."""
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
    """Дописывает новый вариант написания департаменту (обучение алиасов)."""
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
    cur: sqlite3.Cursor, name_en: str, departments: dict[str, dict]
) -> str | None:
    """Создаёт неофициальный департамент без name_ru (заполнит --mode translate)
    или возвращает существующий по имени."""
    existing = find_dept_id_by_name(name_en, departments)
    if existing:
        return existing
    dept_id = generate_dept_id()
    try:
        cur.execute("INSERT INTO departments (id, name_en) VALUES (?, ?)", (dept_id, name_en))
    except sqlite3.IntegrityError:
        cur.execute("SELECT id FROM departments WHERE name_en = ?", (name_en,))
        row = cur.fetchone()
        if not row:
            return None
        dept_id = row[0]
    departments[dept_id] = {"name_en": name_en, "name_ru": None, "name_variants": []}
    return dept_id


# --- Stage 1: точный матч по официальному каталогу ------------------------


def build_official_index(departments: dict[str, dict], official_names: set[str]) -> list[tuple[str, str]]:
    """[(normkey, dept_id)] по name_en/name_ru/variants официальных депов,
    отсортировано по убыванию длины ключа (longest-match-first)."""
    pairs: list[tuple[str, str]] = []
    for dept_id, info in departments.items():
        if info["name_en"] not in official_names:
            continue
        for key in [info["name_en"], info.get("name_ru"), *info["name_variants"]]:
            norm = catalog_normalize(key or "")
            if norm:
                pairs.append((norm, dept_id))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def stage1_match(affiliation: str, index: list[tuple[str, str]]) -> list[str]:
    """Официальные dept_id, чьё название дословно (по границам слов) есть в аффилиации."""
    norm_aff = f" {catalog_normalize(affiliation)} "
    ids: list[str] = []
    seen: set[str] = set()
    for norm_key, dept_id in index:
        if dept_id in seen:
            continue
        if f" {norm_key} " in norm_aff:
            ids.append(dept_id)
            seen.add(dept_id)
    return ids


# --- Stage 2: LLM ---------------------------------------------------------


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
            did = create_department(cur, name, departments)
            if did:
                dept_ids.append(did)
            continue

        name = (dep.get("existing_name_en") or "").strip()
        did = find_dept_id_by_name(name, departments) if name else None
        if did is None:
            fallback = name or (dep.get("extracted_name") or "").strip()
            if fallback:
                did = create_department(cur, fallback, departments)
        if did:
            dept_ids.append(did)
            if dep.get("add_variant_to_existing"):
                add_variant(cur, did, (dep.get("extracted_name") or "").strip(), departments)

    return list(dict.fromkeys(dept_ids))


# --- Перевод name_ru (только неофициальные) ------------------------------

TRANSLATE_SYSTEM_PROMPT = """\
Ты переводишь официальные названия подразделений университета ИТМО с
английского на русский.

Тебе дают пачку департаментов: id и name_en. Для КАЖДОГО верни точный
академический перевод на русский (name_ru) — как подразделение по-настоящему
называется по-русски, а не дословный перевод слово-в-слово.

Короткие аббревиатуры ЗАГЛАВНЫМИ БУКВАМИ (2-6 букв) без пробелов, похожие на
транслитерацию названия русской структуры ИТМО (кафедры/факультета/школы) —
верни их КИРИЛЛИЧЕСКУЮ транслитерацию, а не оставляй латиницей: "PISH" ->
"ПИШ", "FBIT" -> "ФБИТ".

Это ОТЛИЧАЕТСЯ от имени собственного/бренда (фирменное название компании,
программы, лаборатории или хаба; слитное/необычное написание вроде
EnergyLab, GreenTech; узнаваемый нейминг вроде "AI Talent Hub") — такие НЕ
переводи и НЕ транслитерируй, верни name_ru = name_en без изменений.
Примеры: "MTS AI" -> "MTS AI" (а не "Искусственный интеллект в MTS"),
"MWS AI" -> "MWS AI", "AI Talent Hub" -> "AI Talent Hub" (а не "Центр
талантов в области ИИ"), "EnergyLab" -> "EnergyLab" (а не "Энергетическая
лаборатория"). Если не уверен, что у названия есть отдельное официальное
русское имя (и это не аббревиатура русской структуры) — лучше оставь
name_en, чем придумывай правдоподобный, но несуществующий перевод. Обычные
описательные названия подразделений (Faculty/Department/Laboratory/Center +
предметная область) переводи как обычно.

Верни СТРОГО валидный JSON без markdown, объект с ключом "departments" —
массив ровно по числу входных, порядок сохраняй:
{"departments":[{"id":"...","name_ru":"..."}]}
"""


def fetch_untranslated(conn: sqlite3.Connection, official_names: set[str]) -> list[tuple[str, str]]:
    """(id, name_en) неофициальных депов без name_ru или со скопированным name_en.

    Официальные депы (name_en ∈ каталог) исключены: их name_ru — из каталога
    (seed_departments), перезаписывать переводом нельзя.
    """
    cur = conn.cursor()
    cur.execute("SELECT id, name_en, name_ru FROM departments ORDER BY id")
    return [
        (dept_id, name_en)
        for dept_id, name_en, name_ru in cur.fetchall()
        if name_en not in official_names
        and (not name_ru or name_ru.strip().lower() == (name_en or "").strip().lower())
    ]


def call_llm_translate(chunk: list[tuple[str, str]]) -> dict | None:
    lines = [f"id: {dept_id}   name_en: {name_en}" for dept_id, name_en in chunk]
    user_prompt = (
        f"ПАЧКА ДЕПАРТАМЕНТОВ (всего {len(chunk)}):\n" + "\n".join(lines) +
        f"\n\nВерни JSON с массивом ровно из {len(chunk)} объектов, порядок сохраняй."
    )
    return chat_json(
        DEPT_TRANSLATE_MODEL,
        [
            {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )


def run_translate(conn: sqlite3.Connection, cur: sqlite3.Cursor, limit: int | None = None) -> None:
    official_names = official_name_en_set(load_catalog())
    depts = fetch_untranslated(conn, official_names)
    if limit:
        depts = depts[:limit]
    if not depts:
        logger.info("Все неофициальные департаменты уже переведены.")
        return

    chunks = [depts[i : i + DEPT_TRANSLATE_CHUNK_SIZE] for i in range(0, len(depts), DEPT_TRANSLATE_CHUNK_SIZE)]
    logger.info("Перевожу %d департаментов через %s (%d чанков по %d)",
                len(depts), DEPT_TRANSLATE_MODEL, len(chunks), DEPT_TRANSLATE_CHUNK_SIZE)
    stats = {"filled": 0, "empty": 0, "failed_chunks": 0}

    for idx, chunk in enumerate(chunks, 1):
        logger.info("[чанк %d/%d] %d деп.", idx, len(chunks), len(chunk))
        result = call_llm_translate(chunk)
        if result is None:
            stats["failed_chunks"] += 1
            time.sleep(DEPT_TRANSLATE_SLEEP_BETWEEN_CHUNKS)
            continue

        for res in result.get("departments", []):
            dept_id = res.get("id")
            if not dept_id:
                continue
            name_ru = (res.get("name_ru") or "").strip()
            cur.execute("UPDATE departments SET name_ru = ? WHERE id = ?", (name_ru, dept_id))
            stats["filled" if name_ru else "empty"] += 1
        conn.commit()
        time.sleep(DEPT_TRANSLATE_SLEEP_BETWEEN_CHUNKS)

    logger.info(
        "Переведено: %d, пустых: %d, чанков с ошибкой: %d",
        stats["filled"], stats["empty"], stats["failed_chunks"],
    )


# --- Матчинг: оркестрация -------------------------------------------------


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


def stage1_partition(units: list[str], index: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Делит очищенные юниты на (dept_id официальных матчей, несматченные строки)."""
    matched: list[str] = []
    unmatched: list[str] = []
    for unit in units:
        ids = stage1_match(unit, index)
        if ids:
            matched.extend(ids)
        else:
            unmatched.append(unit)
    return list(dict.fromkeys(matched)), unmatched


def run_match(conn: sqlite3.Connection, cur: sqlite3.Cursor) -> None:
    persons = fetch_persons_without_department(conn)
    if not persons:
        logger.info("Все люди с аффилиацией уже размечены департаментами.")
        return

    official_names = official_name_en_set(load_catalog())
    departments = load_departments(cur)
    index = build_official_index(departments, official_names)

    remaining: list[tuple[str, str]] = []
    stage1_hits = no_unit = 0
    for pid, aff in persons:
        units = clean_affiliation(aff)                 # дедуп/очистка аффилиации до юнитов ИТМО
        if not units:
            if has_department_mention(aff):
                remaining.append((pid, aff))           # подразделение упомянуто, но не извлеклось → LLM на сыром
            else:
                cur.execute("UPDATE persons_itmo SET department = ? WHERE id = ?", (NO_DEPT_SENTINEL, pid))
                no_unit += 1
            continue
        matched, unmatched = stage1_partition(units, index)
        if matched and not unmatched:
            cur.execute("UPDATE persons_itmo SET department = ? WHERE id = ?", ("; ".join(matched), pid))
            stage1_hits += 1
        else:
            remaining.append((pid, "; ".join(units)))  # в LLM — короткий чистый список юнитов
    conn.commit()
    logger.info(
        "Stage-1: официально размечено %d/%d, без ITMO-подразделения %d, на LLM осталось %d",
        stage1_hits, len(persons), no_unit, len(remaining),
    )
    if remaining:
        _match_stage2_llm(conn, cur, remaining)


def _match_stage2_llm(
    conn: sqlite3.Connection, cur: sqlite3.Cursor, remaining: list[tuple[str, str]]
) -> None:
    """Stage 2: остаток персон через LLM с якорем на официальные русские названия."""
    chunks = [remaining[i : i + DEPT_CHUNK_SIZE] for i in range(0, len(remaining), DEPT_CHUNK_SIZE)]
    logger.info("Stage-2: %d человек через %s (%d чанков по %d)",
                len(remaining), DEPT_MODEL, len(chunks), DEPT_CHUNK_SIZE)
    stats = {"updated": 0, "no_dept": 0, "failed_chunks": 0}
    depts_start = cur.execute("SELECT COUNT(*) FROM departments").fetchone()[0]

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
            cur.execute("UPDATE persons_itmo SET department = ? WHERE id = ?", (dept_str, pid))
        conn.commit()
        time.sleep(DEPT_SLEEP_BETWEEN_CHUNKS)

    new_depts = cur.execute("SELECT COUNT(*) FROM departments").fetchone()[0] - depts_start
    logger.info(
        "Stage-2: размечено %d, без департамента: %d, новых депов: %d, чанков с ошибкой: %d",
        stats["updated"], stats["no_dept"], new_depts, stats["failed_chunks"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Департаменты ИТМО: 2-этапное сопоставление аффилиаций или перевод названий."
    )
    parser.add_argument("--mode", choices=("match", "translate"), default="match",
                        help="match — сопоставить аффилиации (по умолчанию); "
                             "translate — заполнить name_ru неофициальных депов.")
    parser.add_argument("--limit", type=int, default=None,
                        help="[translate] Сколько департаментов перевести за запуск.")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    try:
        if args.mode == "translate":
            run_translate(conn, cur, args.limit)
        else:
            run_match(conn, cur)
    except Exception:
        logger.exception("enrich_departments упал с ошибкой")
        raise
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    main()
