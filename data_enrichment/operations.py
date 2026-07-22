from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import urlparse

import fitz
import requests

from .config import (
    CLASSIFY_MODEL,
    CONTEXT_RADIUS,
    DEPT_MODEL,
    DEPT_TIMEOUT,
    BROWSER_USER_AGENT,
    CROSSREF_TIMEOUT,
    CROSSREF_URL,
    GITHUB_API_URL,
    GITHUB_COMMIT_PAGES,
    GITHUB_TOKEN,
    MAX_ACCOUNT_REPO_PAGES,
    HTTP_TIMEOUT,
    OPENALEX_API_KEY,
    OPENALEX_AUTHORS_URL,
    OPENREVIEW_API_URL,
    OPENREVIEW_PASSWORD,
    OPENREVIEW_RATE_LIMIT_SLEEP,
    OPENREVIEW_USERNAME,
    ORCID_PUBLIC_API,
    PAGE_SCRAPE_TIMEOUT,
    PERSONS_RU_MODEL,
    TOPICS_LIMIT,
    USER_AGENT,
    USER_AGENT_EMAIL,
    pdf_path_for,
)
from .llm import chat_json
from .operation import PerRecordOperation, WholeSetOperation

logger = logging.getLogger(__name__)


# --- Слой 1 параллельно -------------------------


def alpha(s: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())


def orcid_tail(u: str | None) -> str | None:
    return u.rstrip("/").split("/")[-1] if u else None


def author_surnames(name_en: str, variants: str | None) -> set[str]:
    """Фамилии (последний токен) из name_en + name_variants, длиной >= 4."""
    surnames = set()
    try:
        names = [name_en] + (json.loads(variants) if variants else [])
    except (TypeError, ValueError):
        names = [name_en]
    for n in names:
        toks = [alpha(t) for t in (n or "").split() if alpha(t)]
        if len(toks) >= 2 and len(toks[-1]) >= 4:
            surnames.add(toks[-1])
    return surnames


def surname_match(family: str, surnames: set[str]) -> bool:
    return any(s == family or s in family or family in s for s in surnames)


def crossref_authors(doi: str) -> list[tuple[str, str]]:
    """[(фамилия_alpha, orcid)] по DOI из Crossref."""
    doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
    try:
        r = requests.get(
            f"{CROSSREF_URL}{doi}",
            headers={"User-Agent": f"ITMO-Research/1.0 (mailto:{USER_AGENT_EMAIL})"},
            timeout=CROSSREF_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    try:
        authors = r.json()["message"].get("author", [])
    except (ValueError, KeyError):
        return []
    out = []
    for a in authors:
        orc = orcid_tail(a.get("ORCID"))
        fam = alpha(a.get("family", ""))
        if orc and len(fam) >= 4:
            out.append((fam, orc))
    return out


class CrossrefOrcid(PerRecordOperation):
    """По DOI публикации добирает ORCID авторов из Crossref, привязывает к ИТМО-автору
    по совпадению фамилии. Единица = одна публикация."""
    name = "crossref_orcid"
    uses_external_api = True
    source = "scripts/crossref_orcid.py"

    def pending(self, ctx) -> list:
        # Группируем нуждающихся ИТМО-авторов по публикации: [(pub_id, doi, [(pid, surnames)])].
        by_pub: dict[tuple[str, str], list] = {}
        for pub_id, doi, pid, name_en, variants in ctx.connector.itmo_authors_needing_orcid():
            surn = author_surnames(name_en, variants)
            if surn:
                by_pub.setdefault((pub_id, doi), []).append((pid, surn))
        return [(pub_id, doi, authors) for (pub_id, doi), authors in by_pub.items()]

    def fetch(self, ctx, batch):
        _, doi, _ = batch[0]
        return crossref_authors(doi)

    def save(self, ctx, batch, data) -> None:
        _, doi, authors = batch[0]
        for family, orc in data:
            cand = [pid for pid, surn in authors if surname_match(family, surn)]
            if len(cand) == 1:                       # только однозначное совпадение фамилии
                ctx.connector.save_crossref_orcid(cand[0], orc, doi)


# --- find_code_links: извлечение github-ссылок ---

URL_PATTERN = re.compile(
    r'(?<![\w.])(?:https?://)?(?:www\.)?(github\.com)(/[^\s<>)\]}"\']+)', re.IGNORECASE
)
HYPHEN_LINEBREAK = re.compile(r"(?<=\w)-\n\s*(?=\w)")
URL_LINEBREAK = re.compile(r"(?<=[\w/.\-?&=#%])\n\s*(?=[\w/])")
SENTENCE_START_TAIL = re.compile(r"\.(?:[A-Z][a-z]+[\w-]*|[A-Z]{2,}[\w-]*|\d+(?:\.\d+)*)$")
URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"

SOURCE_TEXT = "pdf_text"
SOURCE_ANNOT = "pdf_annotation"
SOURCE_ABSTRACT = "abstract"
SOURCE_PRIORITY = {SOURCE_TEXT: 0, SOURCE_ANNOT: 1, SOURCE_ABSTRACT: 2}


def split_at_embedded_url(url: str) -> str:
    """Отрезает второй вложенный http(s):// (склейка ссылки и сноски в PDF)."""
    positions = [p for p in (url.find("http://", 1), url.find("https://", 1)) if p > 0]
    return url[: min(positions)] if positions else url


def canonicalize_repo_url(url: str) -> str:
    """Сворачивает github-URL к https://github.com/user/repo (без /tree, .git и т.п.)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    segments = [seg for seg in parsed.path.split("/") if seg]
    if len(segments) < 2:
        return url
    repo = segments[1]
    if ".git" in repo:  # "repo.gitReceived" → "repo"
        repo = repo.split(".git", 1)[0]
    return f"https://github.com/{segments[0]}/{repo}"


def clean_url_tail(url: str) -> str:
    url = SENTENCE_START_TAIL.sub("", url.rstrip(URL_TRAILING_PUNCT))
    url = split_at_embedded_url(url)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return canonicalize_repo_url(url)


def normalize_pdf_text(text: str) -> str:
    return URL_LINEBREAK.sub("", HYPHEN_LINEBREAK.sub("", text))


def is_github(url: str) -> bool:
    try:
        netloc = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return False
    return netloc == "github.com" or netloc.endswith(".github.com")


def find_urls_in_text(text: str) -> list[tuple[str, int, int]]:
    return [(clean_url_tail(m.group(0)), m.start(), m.end()) for m in URL_PATTERN.finditer(text)]


def slice_context(text: str, start: int, end: int) -> str:
    return text[max(0, start - CONTEXT_RADIUS): min(len(text), end + CONTEXT_RADIUS)]


def extract_from_pdf_page(page) -> list[tuple[str, str, str]]:
    """(url, context, source) с одной страницы: видимый текст + гиперссылки."""
    results = []
    raw_text = page.get_text()
    normalized = normalize_pdf_text(raw_text) if raw_text else ""
    for url, start, end in find_urls_in_text(normalized):
        results.append((url, slice_context(normalized, start, end), SOURCE_TEXT))
    for link in page.get_links() or []:
        uri = link.get("uri")
        if not uri or link.get("kind") != fitz.LINK_URI or not is_github(uri):
            continue
        url = clean_url_tail(uri)
        rect = link.get("from")
        visible = ""
        if rect is not None:
            try:
                visible = page.get_text("text", clip=rect).strip()
            except Exception:
                visible = ""
        context = visible or url
        if visible and normalized:
            idx = normalized.find(visible)
            if idx >= 0:
                context = slice_context(normalized, idx, idx + len(visible))
        results.append((url, context, SOURCE_ANNOT))
    return results


def extract_from_pdf(pdf_path: str) -> list[tuple[str, str, int | None, str]]:
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logger.warning("не удалось открыть PDF: %s", exc)
        return []
    results = []
    for page_num, page in enumerate(doc, 1):
        for url, context, source in extract_from_pdf_page(page):
            results.append((url, context, page_num, source))
    doc.close()
    return results


def extract_from_abstract(abstract: str) -> list[tuple[str, str, int | None, str]]:
    if not abstract:
        return []
    normalized = normalize_pdf_text(abstract)
    return [
        (url, slice_context(normalized, start, end), None, SOURCE_ABSTRACT)
        for url, start, end in find_urls_in_text(normalized)
    ]


def deduplicate(links: list[tuple[str, str, int | None, str]]) -> list[tuple[str, str, int | None]]:
    """Одна запись на URL. Приоритет источника: текст PDF > аннотация > абстракт.

    Схлопывание по каноническому URL заодно убирает дубли репозиториев, которые
    раньше разъезжались из-за /tree/main, .git и склеенных хвостов.
    """
    chosen: dict[str, tuple[str, str, int | None, str]] = {}
    for entry in links:
        url = entry[0]
        if url not in chosen or SOURCE_PRIORITY[entry[3]] < SOURCE_PRIORITY[chosen[url][3]]:
            chosen[url] = entry
    return [(url, context, page) for url, context, page, _ in chosen.values()]


class ExtractRepoLinks(PerRecordOperation):
    """Достаёт github-ссылки из PDF и абстракта публикации в repo_links.
    Единица = одна публикация. Внешних вызовов нет — только чтение локальных PDF."""
    name = "extract_repo_links"
    uses_external_api = False
    source = "scripts/find_code_links.py --mode extract"

    def pending(self, ctx) -> list:
        # Все публикации с материалом: запись только досыпает недостающие URL, поэтому
        # перезапуск безопасен и подхватывает PDF, скачанные после прошлого прогона.
        return ctx.connector.publications_for_link_extract()

    def fetch(self, ctx, batch):
        pub_id, abstract = batch[0]
        links = []
        pdf_path = pdf_path_for(pub_id)
        if os.path.exists(pdf_path):
            links = extract_from_pdf(pdf_path)
        if not links and abstract:
            links = extract_from_abstract(abstract)
        return deduplicate(links)

    def save(self, ctx, batch, data) -> None:
        pub_id, _ = batch[0]
        ctx.connector.merge_repo_links(pub_id, data)


CLASSIFY_PROMPT = """Ты помогаешь анализировать научные публикации.

Публикация:
  Название: {title}
  Авторы:   {authors}

В её материалах найдена ссылка:
  URL:  {url}
  {source_hint}

Окружающий текст:
\"\"\"
{context}
\"\"\"

Вопрос: это репозиторий/модель/датасет, который ВЫЛОЖИЛИ САМИ АВТОРЫ этой
статьи как сопроводительный материал — или это упоминание чужого инструмента?

Признаки авторского: "our code is available at", "we release", "наш код доступен";
имя пользователя/организации в URL похоже на автора или его аффилиацию.
Признаки чужого: ссылка в списке литературы; известная чужая библиотека
(PyTorch, BERT, Llama); формулировки "we use", "based on", "following [N]".

Ответь СТРОГО валидным JSON без markdown:
{{"is_authors_artifact": true, "confidence": 0.0, "reason": "одно короткое предложение"}}
"""


class ClassifyRepoLinks(PerRecordOperation):
    """LLM решает, авторская ли ссылка. Единица = одна ссылка."""
    name = "classify_repo_links"
    uses_external_api = True
    source = "scripts/find_code_links.py --mode classify"

    def pending(self, ctx) -> list:
        return ctx.connector.unclassified_repo_links()

    def fetch(self, ctx, batch):
        _, url, context, page, title, authors = batch[0]
        hint = (f"Источник: видимый текст PDF, страница {page}." if page is not None
                else "Источник: абстракт из OpenAlex (контекст ограничен).")
        prompt = CLASSIFY_PROMPT.format(
            title=title or "(без названия)",
            authors=authors or "(авторы не указаны)",
            url=url,
            source_hint=hint,
            context=context or "(контекст пустой)",
        )
        return chat_json(CLASSIFY_MODEL, [{"role": "user", "content": prompt}])

    def save(self, ctx, batch, data) -> None:
        if data is None:
            return
        link_id = batch[0][0]
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        ctx.connector.save_link_classification(
            link_id, bool(data.get("is_authors_artifact")), confidence,
            str(data.get("reason") or "").strip())


class CollectEmailsPdf(PerRecordOperation):
    """Email из текста скачанных PDF, привязка к ИТМО-автору по фамилии в локальной
    части адреса. Единица = одна публикация. Без сети (локальный PDF)."""
    name = "collect_emails_pdf"
    uses_external_api = False  # читает локальный PDF
    source = "scripts/collect_emails.py --source pdf"

    def pending(self, ctx) -> list:
        # [(pub_id, pdf_path, [(pid, surnames)])] по публикациям с существующим PDF.
        by_pub: dict[str, list] = {}
        for pub_id, pid, name_en, variants in ctx.connector.itmo_authors_for_pdf_emails():
            surn = author_surnames(name_en, variants)
            if surn:
                by_pub.setdefault(pub_id, []).append((pid, surn))
        units = []
        for pub_id, authors in by_pub.items():
            path = pdf_path_for(pub_id)
            if os.path.exists(path):
                units.append((pub_id, path, authors))
        return units

    def fetch(self, ctx, batch):
        _, path, _ = batch[0]
        return emails_from_pdf(path)

    def save(self, ctx, batch, data) -> None:
        pub_id, _, authors = batch[0]
        for email in data:
            matched = match_authors(email.split("@")[0], authors)
            if len(matched) == 1:                    # только однозначная привязка по фамилии
                ctx.connector.save_collected_email(matched[0], email, "pdf", pub_id)


NO_DEPT_SENTINEL = "-"

DEPT_SYSTEM_PROMPT = """\
Ты — эксперт по организационной структуре университета ИТМО. Твоя задача —
ВРУЧНУЮ найти соответствия между аффилиациями людей и списком департаментов,
никаких автоматических алгоритмов.

Ты получишь:
1. ПОЛНЫЙ список существующих департаментов ИТМО: id, name_en, variants.
2. ПАЧКУ персон, для каждой — сырое поле affiliation (строки через " \\n ").

ДЕЙСТВИЯ:
1. Выдели в аффилиациях упоминания подразделений ИТМО.
   - Голый университет без подразделения (ITMO University, ITMO, Университет
     ИТМО) — НЕ извлекай.
   - Несколько подразделений в одной строке (через запятую, ';' или "and") —
     извлеки КАЖДОЕ отдельно.
   - Нормализуй: убери хвостовое "ITMO University", лишние запятые/кавычки,
     приведи к Title Case, схлопни двойные пробелы.
2. Сравни каждое извлечённое название с существующими (name_en и variants):
   - Незначительные различия (кавычки, регистр, предлоги, Diagnostic/Diagnostics,
     Center/Centre, Laboratory/Lab) — ЭТО ОДНО И ТО ЖЕ.
   - Перестановка слов (Institute of AI ↔ AI Institute) — ОДНО И ТО ЖЕ.
   - Если одно название — лишь часть другого, более длинного — это РАЗНЫЕ
     подразделения (если сомневаешься — считай разными).
   - Явные опечатки одной и той же лаборатории — совпадение.
   - Нашёл → matched=true, existing_name_en = каноничное имя из списка;
     если написание слегка отличается — add_variant_to_existing=true.
   - Не нашёл → matched=false, is_new=true, предложи new_name_en. Не плоди
     дубликаты внутри чанка: одинаковые по смыслу — один и тот же new_name_en.
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


def load_departments(conn) -> dict[str, dict]:
    """Все департаменты как {id: {'name_en': str, 'name_variants': list}}."""
    return {dept_id: {"name_en": name_en, "name_variants": jloads(variants_raw)}
            for dept_id, name_en, variants_raw in conn.all_departments()}


def format_departments_for_prompt(departments: dict[str, dict]) -> str:
    lines = []
    for dept_id, info in departments.items():
        variants = info["name_variants"]
        variants_str = ", ".join(f'"{v}"' for v in variants) if variants else "—"
        lines.append(f"- id: {dept_id}\n  name_en: {info['name_en']}\n  variants: [{variants_str}]")
    return "\n".join(lines)


def find_dept_id_by_name(name: str, departments: dict[str, dict]) -> str | None:
    """Ищет id департамента по точному (после нормализации) совпадению."""
    target = normalize(name)
    if not target:
        return None
    for dept_id, info in departments.items():
        if normalize(info["name_en"]) == target:
            return dept_id
        if any(normalize(v) == target for v in info["name_variants"]):
            return dept_id
    return None


def add_variant(conn, dept_id: str, variant: str, departments: dict[str, dict]) -> None:
    """Дописывает новый вариант написания департаменту, если его ещё нет."""
    info = departments.get(dept_id)
    if not info or not variant:
        return
    target = normalize(variant)
    if target == normalize(info["name_en"]):
        return
    if target in {normalize(v) for v in info["name_variants"]}:
        return
    info["name_variants"].append(variant)
    conn.update_department_variants(dept_id, json.dumps(info["name_variants"], ensure_ascii=False))


def create_department(conn, name_en: str, departments: dict[str, dict]) -> str | None:
    """Создаёт департамент (или возвращает существующий по имени). Без name_ru —
    перевод названий это отдельная операция."""
    existing = find_dept_id_by_name(name_en, departments)
    if existing:
        return existing
    dept_id = f"dept_{uuid.uuid4().hex[:12]}"
    if not conn.create_department_row(dept_id, name_en):
        dept_id = conn.department_id_by_name_en(name_en)
        if not dept_id:
            return None
    departments[dept_id] = {"name_en": name_en, "name_variants": []}
    return dept_id


def resolve_person_departments(person_res: dict, conn, departments: dict[str, dict]) -> list[str]:
    """Превращает departments одного человека из ответа LLM в список dept_id."""
    dept_ids: list[str] = []
    for dep in person_res.get("departments", []):
        if dep.get("is_new"):
            name = (dep.get("new_name_en") or dep.get("extracted_name") or "").strip()
            if not name:
                continue
            did = create_department(conn, name, departments)
            if did:
                dept_ids.append(did)
            continue
        name = (dep.get("existing_name_en") or "").strip()
        did = find_dept_id_by_name(name, departments) if name else None
        if did is None:
            fallback = name or (dep.get("extracted_name") or "").strip()
            if fallback:
                did = create_department(conn, fallback, departments)
        if did:
            dept_ids.append(did)
            if dep.get("add_variant_to_existing"):
                add_variant(conn, did, (dep.get("extracted_name") or "").strip(), departments)
    return list(dict.fromkeys(dept_ids))


class EnrichDepartments(PerRecordOperation):
    """Сопоставляет аффилиации персон с департаментами ИТМО через LLM, заводит новые.
    Единица = один человек, чанк LLM = 20. ПОСЛЕДОВАТЕЛЬНО (parallel_fetch=False):
    список департаментов растёт по ходу, и следующий батч должен видеть созданные
    предыдущим — иначе наплодит дубликаты."""
    name = "enrich_departments"
    uses_external_api = True  # LLM
    batch_size = 20           # чанк LLM: 20 персон на вызов
    parallel_fetch = False    # батчи зависимы: общий растущий список департаментов
    model = DEPT_MODEL
    source = "scripts/enrich_departments.py"

    def pending(self, ctx) -> list:
        # ФИКС бага (нашёл Артём): '-' не вечная метка — такие персоны пересматриваются.
        return ctx.connector.persons_needing_departments()

    def fetch(self, ctx, batch):
        departments = load_departments(ctx.connector)   # свежий список (батчи последовательны)
        persons_block = "\n---\n".join(
            f"person_id: {pid}\naffiliation: {aff or ''}" for pid, aff in batch)
        user_prompt = (
            f"СПИСОК СУЩЕСТВУЮЩИХ ДЕПАРТАМЕНТОВ:\n{format_departments_for_prompt(departments)}\n\n"
            f"===\nДАННЫЕ ПЕРСОН (всего {len(batch)}):\n{persons_block}\n\n"
            "Вручную сопоставь все подразделения и верни JSON."
        )
        return chat_json(self.model, [
            {"role": "system", "content": DEPT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ], timeout=DEPT_TIMEOUT)

    def save(self, ctx, batch, data) -> None:
        if not data:
            return
        conn = ctx.connector
        departments = load_departments(conn)   # включая созданные предыдущими батчами
        for person_res in data.get("persons", []):
            pid = person_res.get("person_id")
            if not pid:
                continue
            dept_ids = resolve_person_departments(person_res, conn, departments)
            conn.set_person_department(pid, "; ".join(dept_ids) if dept_ids else NO_DEPT_SENTINEL)


RU_SYSTEM_PROMPT = """\
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


class TranslateNames(PerRecordOperation):
    name = "enrich_persons_ru"
    uses_external_api = True  # LLM
    batch_size = 50           # чанк LLM: 50 персон на вызов
    model = PERSONS_RU_MODEL
    source = "scripts/enrich_persons_ru.py"

    def pending(self, ctx) -> list:
        # ФИКС: NULL И пустая строка (метод коннектора), иначе неудачный первый
        # прогон не пересмотрится никогда.
        return ctx.connector.persons_needing_ru_names()

    def fetch(self, ctx, batch):
        lines = []
        for pid, name_en, variants_raw in batch:
            variants = parse_variants(variants_raw)
            variants_str = "; ".join(variants) if variants else "—"
            lines.append(f"id: {pid}   name_en: {name_en or '-'}   variants: {variants_str}")
        user_prompt = (
            f"ПАЧКА ПЕРСОН (всего {len(batch)}):\n" + "\n".join(lines) +
            f"\n\nВерни JSON с массивом ровно из {len(batch)} объектов, порядок сохраняй."
        )
        return chat_json(self.model, [
            {"role": "system", "content": RU_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])

    def save(self, ctx, batch, data) -> None:
        if not data:
            return
        for res in data.get("persons", []):
            pid = res.get("id")
            if not pid:
                continue
            surname = (res.get("surname_ru") or "").strip()
            first = (res.get("first_name_ru") or "").strip()
            second = (res.get("second_name_ru") or "").strip()
            ctx.connector.save_ru_name(pid, surname, first, second)


# --- Слой 2 ------------------------------------------------


# --- enrich_persons: парсинг OpenAlex / ORCID ---

GITHUB_RESERVED = {
    "about", "apps", "collections", "customer-stories", "explore", "features",
    "issues", "join", "login", "marketplace", "new", "notifications", "orgs",
    "pricing", "pulls", "search", "settings", "sponsors", "topics", "trending",
}
_GITHUB_PROFILE_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})", re.IGNORECASE)
_GITHUB_PAGES_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9-]{0,38})\.github\.io", re.IGNORECASE)


def tail_id(url: str | None) -> str | None:
    """Последний сегмент URL - для orcid/openalex/ror id."""
    return url.rstrip("/").split("/")[-1] if url else None


def logins_in(text: str) -> list[str]:
    """Достаёт логины GitHub из произвольного текста."""
    found: list[str] = []
    for pattern in (_GITHUB_PROFILE_RE, _GITHUB_PAGES_RE):
        for match in pattern.finditer(text or ""):
            login = match.group(1).rstrip("-")
            if login and login.lower() not in GITHUB_RESERVED:
                found.append(login)
    seen, unique = set(), []
    for login in found:
        if login.lower() not in seen:
            seen.add(login.lower())
            unique.append(login)
    return unique


def extract_from_person(person: dict) -> tuple[list[dict], list[dict]]:
    """Из ORCID/person: github-логины (findings) + researcher-urls."""
    researcher_urls: list[dict] = []
    sources: list[tuple[str, str]] = []
    for entry in (person.get("researcher-urls") or {}).get("researcher-url", []):
        name = entry.get("url-name")
        url = (entry.get("url") or {}).get("value")
        if url:
            researcher_urls.append({"name": name, "url": url})
            sources.append((url, f"researcher-url:{name}" if name else "researcher-url"))
    for entry in (person.get("external-identifiers") or {}).get("external-identifier", []):
        url = (entry.get("external-id-url") or {}).get("value") or ""
        value = entry.get("external-id-value") or ""
        sources.append((f"{url} {value}", "external-id"))
    for entry in (person.get("keywords") or {}).get("keyword", []):
        if entry.get("content"):
            sources.append((entry["content"], "keyword"))
    bio = (person.get("biography") or {}).get("content")
    if bio:
        sources.append((bio, "biography"))
    findings: dict[str, dict] = {}
    for text, source in sources:
        for login in logins_in(text):
            findings.setdefault(login.lower(), {
                "login": login, "url": f"https://github.com/{login}", "source": source})
    return list(findings.values()), researcher_urls


def parse_openalex_author(a: dict) -> dict:
    """Наукометрия, аффилиации и внешние id из OpenAlex author."""
    ids = a.get("ids") or {}
    stats = a.get("summary_stats") or {}
    affiliations = [
        {
            "name": (aff.get("institution") or {}).get("display_name"),
            "ror": tail_id((aff.get("institution") or {}).get("ror")),
            "country": (aff.get("institution") or {}).get("country_code"),
            "years": aff.get("years") or [],
        }
        for aff in a.get("affiliations") or []
    ]
    last = a.get("last_known_institutions") or []
    topics = [
        {"name": t.get("display_name"), "count": t.get("count"),
         "field": ((t.get("field") or {}).get("display_name"))}
        for t in (a.get("topics") or [])[:TOPICS_LIMIT]
    ]
    return {
        "scopus_id": tail_id(ids.get("scopus")) if ids.get("scopus") else None,
        "twitter": ids.get("twitter"),
        "wikipedia": ids.get("wikipedia"),
        "works_count": a.get("works_count"),
        "cited_by_count": a.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "last_institution": last[0].get("display_name") if last else None,
        "country": last[0].get("country_code") if last else None,
        "affiliations": affiliations,
        "topics": topics,
        "counts_by_year": a.get("counts_by_year") or [],
    }


def _affiliation_rows(group_block: dict, summary_key: str) -> list[dict]:
    """Разбирает блок employments/educations ORCID в список словарей."""
    rows: list[dict] = []
    for group in (group_block or {}).get("affiliation-group", []):
        for summary in group.get("summaries", []):
            e = summary.get(summary_key, {})
            start = (e.get("start-date") or {}).get("year") or {}
            end = (e.get("end-date") or {}).get("year") or {}
            org = e.get("organization") or {}
            addr = org.get("address") or {}
            rows.append({
                "org": org.get("name"), "role": e.get("role-title"),
                "start": start.get("value"), "end": end.get("value"),
                "country": addr.get("country"),
            })
    return rows


def parse_orcid_record(record: dict) -> dict:
    """История работы/учёбы, внешние id, страна, keywords, emails из ORCID."""
    activities = record.get("activities-summary") or {}
    person = record.get("person") or {}
    employments = _affiliation_rows(activities.get("employments"), "employment-summary")
    educations = _affiliation_rows(activities.get("educations"), "education-summary")

    external_ids = []
    scopus_id = researcher_id = linkedin = None
    for x in (person.get("external-identifiers") or {}).get("external-identifier", []):
        ext_type = x.get("external-id-type")
        value = x.get("external-id-value")
        url = (x.get("external-id-url") or {}).get("value")
        external_ids.append({"type": ext_type, "value": value, "url": url})
        low = (ext_type or "").lower()
        if "scopus" in low and not scopus_id:
            scopus_id = value
        elif "researcher" in low and not researcher_id:
            researcher_id = value
        elif "linkedin" in low and not linkedin:
            linkedin = url or value

    _, researcher_urls = extract_from_person(person)
    for ru in researcher_urls:
        if linkedin is None and "linkedin.com" in (ru.get("url") or "").lower():
            linkedin = ru["url"]

    keywords = [k.get("content") for k in (person.get("keywords") or {}).get("keyword", [])
                if k.get("content")]
    bio = (person.get("biography") or {}).get("content")
    country = None
    for addr in (person.get("addresses") or {}).get("address", []):
        country = (addr.get("country") or {}).get("value")
        if country:
            break
    emails = [e.get("email") for e in (person.get("emails") or {}).get("email", [])
              if e.get("email")]

    name = person.get("name") or {}
    other_names = []
    credit = (name.get("credit-name") or {}).get("value")
    if credit:
        other_names.append(credit)
    for o in (person.get("other-names") or {}).get("other-name", []):
        if o.get("content"):
            other_names.append(o["content"])

    return {
        "employments": employments, "educations": educations, "external_ids": external_ids,
        "scopus_id": scopus_id, "researcher_id": researcher_id, "linkedin": linkedin,
        "researcher_urls": researcher_urls, "keywords": keywords, "biography": bio,
        "country": country, "emails": emails, "other_names": other_names,
    }


def _j(value):
    """Сериализует непустую структуру в JSON, иначе None."""
    return json.dumps(value, ensure_ascii=False) if value else None


def _get_json(url, params=None, headers=None, retries=3):
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("запрос упал %s: %s", url, exc)
        return None
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            return None
    if resp.status_code == 429 and retries > 0:
        time.sleep(60)   # запас; с рейт-лимитером 429 редок
        return _get_json(url, params, headers, retries - 1)
    if resp.status_code != 404:
        logger.warning("%d для %s", resp.status_code, url)
    return None


def fetch_openalex_author(author_id: str) -> dict | None:
    params = {"mailto": USER_AGENT_EMAIL}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    return _get_json(f"{OPENALEX_AUTHORS_URL}/{author_id}", params=params,
                     headers={"User-Agent": USER_AGENT})


def fetch_orcid_record(orcid: str) -> dict | None:
    return _get_json(f"{ORCID_PUBLIC_API}/{orcid}/record", headers={"Accept": "application/json"})


# Колонки person_profiles, которые хранятся как JSON (сериализуются одним проходом).
_JSON_COLS = {
    "affiliations", "employments", "educations", "topics", "counts_by_year",
    "researcher_urls", "external_ids", "keywords", "emails", "other_names", "github_urls",
}


def build_profile_row(person_id, name_en, author_id, oa, orc, github, status) -> dict:
    """Собирает строку person_profiles (ключи = PERSON_PROFILE_COLS в коннекторе)."""
    o, r = oa or {}, orc or {}
    row = {
        "person_id": person_id, "name_en": name_en,
        "openalex_author_id": author_id,
        "openalex_url": f"https://openalex.org/{author_id}",
        "status": status,
        "has_github": 1 if github else 0,
        "github_urls": [g["url"] for g in github],
        # прямо из OpenAlex
        "twitter": o.get("twitter"), "wikipedia": o.get("wikipedia"),
        "works_count": o.get("works_count"), "cited_by_count": o.get("cited_by_count"),
        "h_index": o.get("h_index"), "i10_index": o.get("i10_index"),
        "last_institution": o.get("last_institution"), "affiliations": o.get("affiliations"),
        "topics": o.get("topics"), "counts_by_year": o.get("counts_by_year"),
        # прямо из ORCID
        "orcid": r.get("orcid"), "researcher_id": r.get("researcher_id"),
        "linkedin": r.get("linkedin"), "employments": r.get("employments"),
        "educations": r.get("educations"), "researcher_urls": r.get("researcher_urls"),
        "external_ids": r.get("external_ids"), "keywords": r.get("keywords"),
        "biography": r.get("biography"), "emails": r.get("emails"),
        "other_names": r.get("other_names"),
        # приоритет ORCID -> OpenAlex
        "scopus_id": r.get("scopus_id") or o.get("scopus_id"),
        "country": r.get("country") or o.get("country"),
    }
    return {k: (_j(v) if k in _JSON_COLS else v) for k, v in row.items()}


class EnrichPersons(PerRecordOperation):
    """OpenAlex /authors + ORCID /record -> person_profiles. ORCID берётся из OpenAlex,
    иначе fallback из crossref (порядок crossref -> enrich_persons, петли нет).
    Единица = один человек, два внешних вызова."""
    name = "enrich_persons"
    uses_external_api = True  # OpenAlex + ORCID
    source = "scripts/enrich_persons.py"

    def pending(self, ctx) -> list:
        # Новые + переобработка no_orcid, у кого появился crossref-ORCID.
        cr = dict(ctx.connector.crossref_orcid_map())
        units = []
        for pid, name_en in ctx.connector.persons_to_enrich():
            author_id = pid[len("itmo_"):] if pid.startswith("itmo_") else pid
            units.append((pid, name_en, author_id, cr.get(pid)))
        return units

    def fetch(self, ctx, batch):
        _, _, author_id, cr_orcid = batch[0]
        author = fetch_openalex_author(author_id)   # рейт-лимитер уже запущен фреймворком
        oa = parse_openalex_author(author) if author else {}
        orcid = tail_id((author or {}).get("orcid")) or cr_orcid
        orc, github, status = {}, [], "no_orcid"
        if orcid:
            if ctx.rate_limiter:
                ctx.rate_limiter.acquire()          # второй внешний вызов
            record = fetch_orcid_record(orcid)
            if record is None:
                status = "orcid_error"
            else:
                orc = parse_orcid_record(record)
                orc["orcid"] = orcid
                github, _ = extract_from_person(record.get("person") or {})
                status = "enriched"
        return {"author_id": author_id, "oa": oa, "orc": orc, "github": github, "status": status}

    def save(self, ctx, batch, data) -> None:
        person_id, name_en, _, _ = batch[0]
        row = build_profile_row(person_id, name_en, data["author_id"],
                                data["oa"], data["orc"], data["github"], data["status"])
        ctx.connector.save_person_profile(row)


# --- Слой 3 два независимых потока параллельно ----------------------


LINK_KEYS = ("homepage", "gscholar", "dblp", "orcid", "github", "linkedin")

def norm(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"[^a-zа-я0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()

def tail(v: str | None) -> str | None:
    return v.rstrip("/").split("/")[-1] if v else None

def jloads(s):
    try:
        return json.loads(s) if s else []
    except (TypeError, ValueError):
        return []

def search_terms(name_en: str):
    """Имя целиком, затем «Имя Фамилия» без инициалов."""
    yield name_en
    toks = [t for t in name_en.split() if len(t.strip(".")) > 1]
    if len(toks) >= 2 and f"{toks[0]} {toks[-1]}" != name_en:
        yield f"{toks[0]} {toks[-1]}"

class OpenReviewClient:
    """Логин в OpenReview один раз + поиск профилей по имени."""

    def __init__(self) -> None:
        self.s = requests.Session()
        r = self.s.post(
            f"{OPENREVIEW_API_URL}/login",
            json={"id": OPENREVIEW_USERNAME, "password": OPENREVIEW_PASSWORD},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['token']}"

    def search(self, term: str) -> list[dict]:
        try:
            r = self.s.get(
                f"{OPENREVIEW_API_URL}/profiles/search", params={"term": term},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return []
        if r.status_code == 429:
            time.sleep(OPENREVIEW_RATE_LIMIT_SLEEP)
            return self.search(term)
        if r.status_code != 200:
            return []
        return r.json().get("profiles", [])

def verify(content: dict, name_norms: set[str], our_orcid: str | None) -> str | None:
    """Возвращает способ подтверждения принадлежности ИТМО или None."""
    cand_orcid = tail(content.get("orcid"))
    if our_orcid and cand_orcid and our_orcid == cand_orcid:
        return "orcid"
    cand_names = {norm(n.get("fullname")) for n in content.get("names", []) if n.get("fullname")}
    if not (cand_names & name_norms):
        return None
    emails = content.get("emails") or []
    if any((e or "").lower().endswith("@itmo.ru") for e in emails):
        return "itmo_email"
    if any("itmo" in ((h.get("institution") or {}).get("name") or "").lower()
           for h in content.get("history", [])):
        return "itmo_affil"
    return None


def extract(profile: dict, matched_by: str, name_en: str) -> dict:
    c = profile.get("content", {})
    links = {k: c.get(k) for k in LINK_KEYS}
    return {
        "openreview_id": profile.get("id"),
        "name_en": name_en,
        "matched_by": matched_by,
        "names": [n.get("fullname") for n in c.get("names", []) if n.get("fullname")],
        "emails_masked": c.get("emails") or [],
        "affiliations": [
            {"name": (h.get("institution") or {}).get("name"),
             "position": h.get("position"), "start": h.get("start"), "end": h.get("end")}
            for h in c.get("history", [])
        ],
        "relations": [
            {"relation": r.get("relation"), "name": r.get("name")}
            for r in c.get("relations", [])
        ],
        **links,
    }

class EnrichOpenreview(PerRecordOperation):
    """Ищет профиль на OpenReview по имени, подтверждает принадлежность ИТМО по
    ORCID / @itmo.ru / аффилиации. ORCID не обязателен — один из трёх сигналов
    (потому идёт после enrich_persons: с ORCID находок больше). Единица = человек."""
    name = "enrich_openreview"
    uses_external_api = True  # OpenReview API
    source = "scripts/enrich_openreview.py"

    def pending(self, ctx) -> list:
        self._client = OpenReviewClient()   # логин один раз, в главном потоке
        conn = ctx.connector
        pdata = {pid: (tail(o), jloads(on)) for pid, o, on in conn.openreview_person_data()}
        done = {r[0] for r in conn.openreview_done()}
        units = []
        for pid, name_en, variants in conn.persons_names_variants():
            if pid in done:
                continue
            our_orcid, others = pdata.get(pid, (None, []))
            norms = {norm(name_en)} | {norm(n) for n in jloads(variants) + others}
            extra = [n for n in others if norm(n) and norm(n) != norm(name_en)]
            units.append((pid, name_en, {n for n in norms if n}, extra, our_orcid))
        return units

    def fetch(self, ctx, batch):
        _, name_en, norms, extra, our_orcid = batch[0]
        for term in list(search_terms(name_en)) + extra:
            if ctx.rate_limiter:
                ctx.rate_limiter.acquire()
            for profile in self._client.search(term):
                matched_by = verify(profile.get("content", {}), norms, our_orcid)
                if matched_by:
                    return extract(profile, matched_by, name_en)
        return None

    def save(self, ctx, batch, data) -> None:
        if data:
            ctx.connector.save_openreview_profile(batch[0][0], data)

# --- collect_emails --pages ---

TLD = (r"(?:ru|com|org|net|edu|gov|io|info|biz|name|eu|de|fr|uk|us|cn|jp|kr"
       r"|in|it|es|nl|se|fi|no|ch|at|cz|pl|by|kz|ua)")
EMAIL_RE = re.compile(rf"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+?\.{TLD}(?![A-Za-z])", re.I)
BRACE_RE = re.compile(rf"\{{([^{{}}@]+)\}}@([A-Za-z0-9.\-]+?\.{TLD})(?![A-Za-z])", re.I)  # {a,b}@domain
MAILTO_RE = re.compile(r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.I)

SKIP_HOSTS = ("github.com", "linkedin.com", "scholar.google", "researchgate",
              "orcid.org", "twitter.com", "x.com", "facebook", "youtube",
              "t.me", "vk.com", "semanticscholar", "dblp.org", "publons")


def match_authors(local_part: str, authors: list[tuple[str, set[str]]]) -> list[str]:
    """person_id, чья фамилия встречается в локальной части адреса."""
    lp = alpha(local_part)
    if not lp:
        return []
    return [pid for pid, surnames in authors if any(s in lp for s in surnames)]


# def save(out: sqlite3.Connection, pid: str, email: str, source: str, ref: str | None) -> None:
#     out.execute(
#         "INSERT OR IGNORE INTO collected_emails (person_id, email, source, ref) VALUES (?, ?, ?, ?)",
#         (pid, email, source, ref),
#     )


# --- Источник: PDF ---------------------------------------------------------


def emails_from_pdf_text(text: str) -> set[str]:
    found = set()
    for inside, dom in BRACE_RE.findall(text):
        for part in re.split(r"[;,]", inside):
            part = part.strip().strip(".")
            if part:
                found.add(f"{part}@{dom}".lower())
    for m in EMAIL_RE.findall(text):
        found.add(m.lower().rstrip("."))
    return found


def emails_from_pdf(path) -> set[str]:
    try:
        doc = fitz.open(path)
    except Exception:
        return set()
    out: set[str] = set()
    for page in doc:
        out |= emails_from_pdf_text(page.get_text() or "")
    doc.close()
    return out


# def load_pub_authors(main: sqlite3.Connection) -> dict[str, list[tuple[str, set[str]]]]:
#     """publication_id -> [(person_id, {surnames})] по ИТМО-авторам."""
#     rows = main.execute(
#         """
#         SELECT pa.publication_id, p.id, p.name_en, p.name_variants
#         FROM publication_authors pa
#         JOIN persons_itmo p ON p.id = pa.person_id
#         WHERE pa.person_type = 'itmo'
#         """
#     ).fetchall()
#     by_pub: dict[str, list[tuple[str, set[str]]]] = {}
#     for pub_id, pid, name_en, variants in rows:
#         surn = author_surnames(name_en, variants)
#         if surn:
#             by_pub.setdefault(pub_id, []).append((pid, surn))
#     return by_pub


# def run_pdf(main: sqlite3.Connection, out: sqlite3.Connection) -> None:
#     by_pub = load_pub_authors(main)
#     pubs = [(pid, pdf_path_for(pid)) for pid in by_pub]
#     pubs = [(pid, path) for pid, path in pubs if path.exists()]
#     logger.info("Статей с PDF и ИТМО-авторами: %d", len(pubs))

#     stats = {"pdfs": 0, "emails": 0, "attributed": 0, "ambiguous": 0, "unmatched": 0}
#     for i, (pub_id, path) in enumerate(pubs, 1):
#         stats["pdfs"] += 1
#         emails = emails_from_pdf(path)
#         authors = by_pub[pub_id]
#         for email in emails:
#             stats["emails"] += 1
#             matched = match_authors(email.split("@")[0], authors)
#             if len(matched) == 1:
#                 save(out, matched[0], email, "pdf", pub_id)
#                 stats["attributed"] += 1
#             elif len(matched) > 1:
#                 stats["ambiguous"] += 1
#             else:
#                 stats["unmatched"] += 1
#         if stats["pdfs"] % 100 == 0:
#             out.commit()
#             logger.info("[%d/%d] обработано", i, len(pubs))
#     out.commit()

#     logger.info("PDF прочитано: %d, Email встречено: %d, Привязано (1 автор): %d, Неоднозначных: %d, Без совпадения ФИО: %d",
#                  stats['pdfs'], stats['emails'], stats['attributed'], stats['ambiguous'], stats['unmatched'])

def is_page(u: str | None) -> bool:
    u = (u or "").lower()
    return u.startswith("http") and not any(s in u for s in SKIP_HOSTS)


def deobfuscate(t: str) -> str:
    t = re.sub(r"&#0*64;|&#x0*40;|&commat;", "@", t, flags=re.I)
    t = re.sub(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", ".", t, flags=re.I)
    t = re.sub(r"\s*@\s*", "@", t)
    return t


def emails_from_html(html: str) -> set[str]:
    found = {m.lower() for m in MAILTO_RE.findall(html)}
    found |= {m.lower() for m in EMAIL_RE.findall(deobfuscate(html))}
    return found


# def load_person_urls(main: sqlite3.Connection, prof: sqlite3.Connection) -> dict[str, set[str]]:
#     urls: dict[str, set[str]] = {}
#     for pid, ru in prof.execute(
#         "SELECT person_id, researcher_urls FROM person_profiles WHERE researcher_urls IS NOT NULL"
#     ):
#         for u in (x.get("url") for x in jloads(ru)):
#             if is_page(u):
#                 urls.setdefault(pid, set()).add(u)
#     for pid, hp in prof.execute(
#         "SELECT person_id, homepage FROM openreview_profiles WHERE homepage > ''"
#     ):
#         if is_page(hp):
#             urls.setdefault(pid, set()).add(hp)
#     return urls


# def run_pages(main: sqlite3.Connection, out: sqlite3.Connection, limit: int | None) -> None:
#     surn = {pid: author_surnames(name_en, variants) for pid, name_en, variants in
#             main.execute("SELECT id, name_en, name_variants FROM persons_itmo WHERE name_en > ''")}
#     person_urls = load_person_urls(main, out)
#     done = {r[0] for r in out.execute(
#         "SELECT DISTINCT person_id FROM collected_emails WHERE source = 'page'")}

#     people = [(pid, us) for pid, us in person_urls.items()
#               if pid in surn and surn[pid] and pid not in done]
#     if limit:
#         people = people[:limit]
#     logger.info("Персон к обходу: %d", len(people))

#     session = requests.Session()
#     session.headers["User-Agent"] = BROWSER_USER_AGENT
#     stats = {"persons": 0, "urls": 0, "found": 0}
#     for i, (pid, urls) in enumerate(people, 1):
#         stats["persons"] += 1
#         for url in urls:
#             stats["urls"] += 1
#             try:
#                 r = session.get(url, timeout=PAGE_SCRAPE_TIMEOUT, allow_redirects=True)
#             except requests.RequestException:
#                 continue
#             if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "").lower():
#                 continue
#             for email in emails_from_html(r.text):
#                 if any(s in alpha(email.split("@")[0]) for s in surn[pid]):
#                     save(out, pid, email, "page", url)
#                     stats["found"] += 1
#             time.sleep(PAGE_SCRAPE_REQUEST_DELAY)
#         if stats["persons"] % 25 == 0:
#             out.commit()
#             logger.info("[%d/%d] найдено: %d", i, len(people), stats['found'])
#     out.commit()

#     logger.info("Персон обойдено: %d, URL проверено: %d, Привязок email: %d", stats['persons'], stats['urls'], stats['found'])


class CollectEmailsPages(PerRecordOperation):
    """Email с личных/лаб-страниц (адрес из researcher_urls ORCID + homepage OpenReview),
    деобфускация, привязка по фамилии в локальной части. Единица = один человек.
    Зависит по порядку: нужен openreview_profiles (после enrich_openreview)."""
    name = "collect_emails_pages"
    uses_external_api = True  # скрейп страниц
    source = "scripts/collect_emails.py --source pages"

    def pending(self, ctx) -> list:
        conn = ctx.connector
        surn = {pid: author_surnames(name_en, variants)
                for pid, name_en, variants in conn.persons_names_variants()}
        urls: dict[str, set] = {}
        for pid, ru in conn.researcher_urls_rows():
            for u in (x.get("url") for x in jloads(ru)):
                if is_page(u):
                    urls.setdefault(pid, set()).add(u)
        for pid, hp in conn.openreview_homepages():
            if is_page(hp):
                urls.setdefault(pid, set()).add(hp)
        done = {r[0] for r in conn.pages_done()}
        return [(pid, sorted(us), surn[pid]) for pid, us in urls.items()
                if pid in surn and surn[pid] and pid not in done]

    def fetch(self, ctx, batch):
        _, page_urls, _ = batch[0]
        session = requests.Session()
        session.headers["User-Agent"] = BROWSER_USER_AGENT
        found = []  # (email, url)
        for url in page_urls:
            if ctx.rate_limiter:
                ctx.rate_limiter.acquire()
            try:
                r = session.get(url, timeout=PAGE_SCRAPE_TIMEOUT, allow_redirects=True)
            except requests.RequestException:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "").lower():
                continue
            for email in emails_from_html(r.text):
                found.append((email, url))
        return found

    def save(self, ctx, batch, data) -> None:
        pid, _, surnames = batch[0]
        for email, url in data:
            if any(s in alpha(email.split("@")[0]) for s in surnames):
                ctx.connector.save_collected_email(pid, email, "page", url)


# --- match_github ---


NAME_EXACT = 0.999
NAME_FUZZY = 0.86

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_sim(a: str, b: str) -> float:
    """Сходство двух нормализованных имён: равенство множеств токенов или ratio."""
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    if len(ta) >= 2 and ta == tb:
        return 1.0

    return SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()


def best_name_sim(cands: set[str], persons: set[str]) -> tuple[float, tuple[str, str]]:
    best, pair = 0.0, ("", "")
    for c in cands:
        for p in persons:
            s = name_sim(c, p)
            if s > best:
                best, pair = s, (c, p)
    return best, pair


def login_has_surname(login: str, surname: str | None) -> bool:
    """Логин содержит фамилию персоны"""
    if not surname:
        return False
    low = login.lower()
    return surname in low or SequenceMatcher(None, low, surname).ratio() >= 0.85


def norm_email(e: str | None) -> str:
    return (e or "").strip().lower()

def repo_owner(url: str | None) -> str | None:
    m = re.search(r"github\.com/([^/]+)/", url or "")
    return m.group(1).lower() if m else None

def score_person(cand: dict, person: dict, email_hit: bool):
    """Возвращает (score, signals, evidence) для пары логин<->персона."""
    signals: list[str] = []
    evidence: dict = {}

    if email_hit:
        signals.append("email_exact")
        evidence["email"] = sorted(cand["emails"] & person["emails"])

    sim, pair = best_name_sim(cand["names"], person["names"])
    if sim >= NAME_EXACT:
        signals.append("name_exact")
    elif sim >= NAME_FUZZY:
        signals.append("name_fuzzy")
    if sim >= NAME_FUZZY:
        evidence["name_pair"], evidence["name_sim"] = pair, round(sim, 2)

    itmo_email = any(e.endswith("@itmo.ru") for e in cand["emails"])
    if cand["itmo_text"]:
        signals.append("itmo_profile")
    if itmo_email:
        signals.append("itmo_email")

    if login_has_surname(cand["login"], person.get("surname")):
        signals.append("login_surname")
    if cand["is_owner"]:
        signals.append("owner")
    if cand["org_itmo"]:
        signals.append("org_itmo")

    weights = {"email_exact": 1.0, "name_exact": 0.6, "name_fuzzy": 0.4,
               "itmo_profile": 0.2, "itmo_email": 0.3, "login_surname": 0.3,
               "owner": 0.3, "org_itmo": 0.3}
    score = min(1.0, sum(weights[s] for s in signals))
    return score, signals, evidence


def decide(signals: list[str], in_bridge: bool) -> str:
    if "email_exact" in signals:
        return "matched"
    corrob = any(s in signals for s in
                 ("itmo_profile", "itmo_email", "login_surname", "owner", "org_itmo"))
    if "name_exact" in signals:
        if in_bridge:
            return "matched"
        return "matched" if corrob else "review"
    if "name_fuzzy" in signals:
        if in_bridge and corrob:
            return "matched"
        if in_bridge:
            return "review"
        return "rejected"
    return "rejected"


def match_login(login, cand, persons, email_index, name_index, bridge):
    """Лучшая персона для одного логина: (person_id, score, signals, evidence)."""
    bridge_pids = set()
    for pub in cand["pub_ids"]:
        bridge_pids |= bridge.get(pub, set())

    email_pids = {email_index[e] for e in cand["emails"] if e in email_index}

    global_pids = set()
    for n in cand["names"]:
        if len(n.split()) >= 2:
            global_pids |= name_index.get(n, set())

    best = None
    matched = []
    for pid in bridge_pids | email_pids | global_pids:
        if pid not in persons:
            continue
        in_bridge = pid in bridge_pids
        score, signals, evidence = score_person(cand, persons[pid], pid in email_pids)
        d = decide(signals, in_bridge)
        if d == "rejected":
            continue
        evidence["in_bridge"] = in_bridge
        rank = (d == "matched", score)
        if best is None or rank > best[0]:
            best = (rank, pid, score, signals, evidence, d)
        if d == "matched":
            matched.append((score, pid))
    if best is None:
        return None
    _, pid, score, signals, evidence, d = best
    if d == "matched":
        top = max(s for s, _ in matched)
        if len({p for s, p in matched if s == top}) > 1:
            d = "review"
            evidence["ambiguous"] = True
    return pid, score, signals, evidence, d


def load_persons(ctx):
    """person_id -> {names, emails, name_en, surname, github} + индексы email/имя -> pid."""
    persons: dict[str, dict] = {}
    for pid, name_en, variants, email, github in ctx.connector.persons_for_matching():
        names = {norm_name(name_en)} if name_en else set()
        for v in jloads(variants):
            names.add(norm_name(v))
        toks = norm_name(name_en).split()
        persons[pid] = {
            "names": {n for n in names if n},
            "emails": {e for e in {norm_email(email)} if e},
            "name_en": name_en,
            "surname": toks[-1] if toks and len(toks[-1]) >= 4 else None,
            "github": github,
        }

    for pid, emails_j, other_j in ctx.connector.profiles_for_matching():
        if pid not in persons:
            continue
        persons[pid]["emails"].update(norm_email(e) for e in jloads(emails_j))
        persons[pid]["names"].update(norm_name(o) for o in jloads(other_j))
        persons[pid]["emails"].discard("")
        persons[pid]["names"].discard("")

    email_index: dict[str, str] = {}
    name_index: dict[str, set[str]] = {}
    for pid, p in persons.items():
        for e in p["emails"]:
            email_index.setdefault(e, pid)
        for n in p["names"]:
            if len(n.split()) >= 2:
                name_index.setdefault(n, set()).add(pid)
    return persons, email_index, name_index


def load_pub_authors(ctx) -> dict[str, set[str]]:
    """publication_id -> {itmo-person_id}."""
    bridge: dict[str, set[str]] = {}
    for pub_id, pid in ctx.connector.itmo_publication_authors():
        bridge.setdefault(pub_id, set()).add(pid)
    return bridge


def load_candidates(ctx, itmo_orgs: set[str]) -> dict[str, dict]:
    """github_login -> агрегат по всем его репозиториям."""
    logins: dict[str, dict] = {}
    for (login, url, source, repo_url, pub_ids_j, gh_name, gh_email,
         gh_company, gh_location, gh_bio, commit_emails_j, commit_names_j) in \
            ctx.connector.all_github_candidates():
        c = logins.setdefault(login, {
            "login": login, "url": url, "names": set(), "emails": set(),
            "itmo_text": False, "org_itmo": False,
            "pub_ids": set(), "repos": set(), "is_owner": False,
        })
        c["url"] = url or c["url"]
        c["repos"].add(repo_url)
        c["pub_ids"].update(jloads(pub_ids_j))
        if source == "repo_owner":
            c["is_owner"] = True
        for n in (gh_name, login, *jloads(commit_names_j)):
            if norm_name(n):
                c["names"].add(norm_name(n))
        for e in (gh_email, *jloads(commit_emails_j)):
            if norm_email(e):
                c["emails"].add(norm_email(e))
        blob = f"{gh_company or ''} {gh_location or ''} {gh_bio or ''}".lower()
        if "itmo" in blob or "saint petersburg" in blob or "sankt" in blob:
            c["itmo_text"] = True
        if repo_owner(repo_url) in itmo_orgs:
            c["org_itmo"] = True
    return logins


def match_confidence(signals: list[str], evidence: dict) -> str:
    strong = ("email_exact", "login_surname", "owner")
    return "high" if evidence.get("in_bridge") or any(s in signals for s in strong) else "probable"


class MatchGithub(WholeSetOperation):
    """Идентификация: github аккаунт <-> персона. Двухфазно:
    фаза 1 - глобальные индексы и скоринг всех логинов;
    фаза 2 - атомарно зафиксировать решения."""
    name = "match_github"
    uses_external_api = False
    source = "scripts/match_github.py"

    def process_all(self, ctx) -> int:
        persons, email_index, name_index = load_persons(ctx)
        bridge = load_pub_authors(ctx)
        logins = load_candidates(ctx, ctx.connector.itmo_github_orgs())
        repo_id_by_url = ctx.connector.repository_ids_by_url()
        logger.info("[%s] логинов: %d, персон ИТМО: %d", self.name, len(logins), len(persons))

        # Фаза 1: скоринг без записи. Порядок по логину — решение не зависит от
        # порядка обхода, поэтому результат прогона воспроизводим.
        decisions = []
        for login, cand in sorted(logins.items()):
            result = match_login(login, cand, persons, email_index, name_index, bridge)
            if result is not None:
                decisions.append((login, cand, *result))

        # Фаза 2: одна точка записи. Всё идёт через один коннектор, поэтому прежней
        # блокировки между двумя соединениями на один файл больше нет.
        ctx.connector.clear_github_matches()
        stats = {"matched": 0, "review": 0}
        new_github = 0
        for login, cand, pid, score, signals, evidence, decision in decisions:
            ctx.connector.save_github_match((
                pid, persons[pid]["name_en"], login, cand["url"], score,
                json.dumps(signals), json.dumps(evidence, ensure_ascii=False),
                decision, match_confidence(signals, evidence), json.dumps(sorted(cand["repos"]))))
            stats[decision] = stats.get(decision, 0) + 1
            if decision != "matched":
                continue
            if not persons[pid]["github"]:
                new_github += 1
                persons[pid]["github"] = login  # второй логин той же персоны не считаем новым
            ctx.connector.set_person_github(pid, login)
            role = "owner" if cand["is_owner"] else "contributor"
            for repo_url in cand["repos"]:
                repo_id = repo_id_by_url.get(repo_url)
                if repo_id:
                    ctx.connector.link_repository_person(repo_id, pid, role)

        logger.info("[%s] matched %d, review %d, без цели %d, новых github %d",
                    self.name, stats["matched"], stats["review"],
                    len(logins) - len(decisions), new_github)
        # Цикл соцграфа сходится по числу НОВЫХ привязок: 0 — расширять больше нечем.
        return new_github


BOT_LOGINS = {"web-flow", "github-actions"}


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Из https://github.com/owner/repo достаёт (owner, repo). Иначе None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if "github.com" not in parsed.netloc.lower():
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return None
    return segments[0], segments[1].removesuffix(".git")


def is_bot(login: str | None, user_type: str | None = None) -> bool:
    if not login:
        return True
    low = login.lower()
    return low in BOT_LOGINS or low.endswith("[bot]") or user_type == "Bot"


class GitHubClient:
    """Клиент GitHub REST API: токен, основной и вторичный rate-limit, ретраи."""

    def __init__(self) -> None:
        self.session = requests.Session()
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        else:
            logger.warning("GITHUB_TOKEN не задан — лимит 60 запросов/час")
        self.session.headers.update(headers)
        self.calls = 0

    def _request(self, path: str, params: dict | None, retries: int):
        url = path if path.startswith("http") else f"{GITHUB_API_URL}{path}"
        self.calls += 1
        try:
            resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("запрос упал %s: %s", url, exc)
            return None
        if resp.status_code in (403, 429) and retries > 0:
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                wait = max(0, int(resp.headers.get("X-RateLimit-Reset", "0")) - int(time.time())) + 2
                logger.warning("rate-limit исчерпан, пауза %d сек", min(wait, 3600))
                time.sleep(min(wait, 3600))
            else:                                   # вторичный лимит
                wait = int(resp.headers.get("Retry-After", "60"))
                logger.warning("вторичный лимит, пауза %d сек", wait)
                time.sleep(wait)
            return self._request(path, params, retries - 1)
        return resp

    def get(self, path: str, params: dict | None = None, retries: int = 3):
        """Распарсенный JSON или None."""
        resp = self._request(path, params, retries)
        if resp is None or resp.status_code != 200:
            if resp is not None and resp.status_code not in (200, 404):
                logger.warning("%d для %s", resp.status_code, path)
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def exists(self, path: str) -> bool:
        """Есть ли ресурс (для readme, где тело не нужно)."""
        resp = self._request(path, None, 3)
        return resp is not None and resp.status_code == 200


def repo_id_for(url: str) -> str:
    """Детерминированный id репозитория из URL (для идемпотентности)."""
    return "repo_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def ghdept_id_for(login: str) -> str:
    return "ghdept_" + hashlib.sha1(login.lower().encode("utf-8")).hexdigest()[:12]


def normalize_person_name(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def person_name_cache(ctx) -> dict[str, str]:
    """{нормализованное имя: person_id} по name_en и name_variants."""
    cache: dict[str, str] = {}
    for pid, name_en, variants in ctx.connector.persons_names_variants():
        if name_en:
            cache.setdefault(normalize_person_name(name_en), pid)
        for v in jloads(variants):
            cache.setdefault(normalize_person_name(v), pid)
    return cache


class BuildRepositories(PerRecordOperation):
    """По подтверждённой авторской ссылке заводит репозиторий: метаданные из GitHub,
    организацию-владельца, связи с публикациями и персонами. Единица = один репозиторий."""
    name = "build_repositories"
    uses_external_api = True
    source = "scripts/build_repositories.py"

    def pending(self, ctx) -> list:
        self._names = person_name_cache(ctx)
        self._gh = GitHubClient()
        return ctx.connector.confirmed_repos_not_built()

    def fetch(self, ctx, batch):
        url, _ = batch[0]
        parsed = parse_repo_url(url)
        if parsed is None:
            return None                      # github.com/<только-owner> — мусор
        owner, name = parsed
        meta = self._gh.get(f"/repos/{owner}/{name}")
        if meta is None:                     # удалён/приватный — сохраним минимум
            return {"owner": owner, "name": name, "meta": None}
        owner_obj = meta.get("owner") or {}
        is_org = owner_obj.get("type") == "Organization"
        return {
            "owner": owner, "name": name, "meta": meta, "owner_obj": owner_obj,
            "is_org": is_org,
            "owner_details": self._gh.get(f"/users/{owner_obj.get('login')}") or {},
            "has_readme": self._gh.exists(f"/repos/{owner}/{name}/readme"),
            "contributors": [c["login"] for c in
                             (self._gh.get(f"/repos/{owner}/{name}/contributors",
                                           params={"per_page": 100}) or []) if c.get("login")],
        }

    def save(self, ctx, batch, data) -> None:
        if data is None:
            return
        url, pub_ids = batch[0]
        conn = ctx.connector
        repo_id = repo_id_for(url)
        meta = data["meta"] or {}

        if data["meta"] is None:
            conn.save_repository((repo_id, data["name"], url, None,
                                  date.today().isoformat(), None, data["owner"],
                                  None, None, 0, None, None, None, None))
            for pub_id in pub_ids:
                conn.link_repository_publication(repo_id, pub_id)
            return

        owner_obj, details = data["owner_obj"], data["owner_details"]
        ghdept_id = None
        if data["is_org"]:
            ghdept_id = ghdept_id_for(owner_obj["login"])
            conn.upsert_github_department((
                ghdept_id, owner_obj["login"], details.get("name"),
                owner_obj.get("html_url"), details.get("description"),
                details.get("location"), (details.get("created_at") or "")[:10] or None))

        license_obj = meta.get("license") or {}
        contributors = data["contributors"]
        conn.save_repository((
            repo_id, data["name"], url, meta.get("description"), date.today().isoformat(),
            json.dumps(contributors, ensure_ascii=False) if contributors else None,
            data["owner"], "org" if data["is_org"] else "user", ghdept_id,
            1 if data["has_readme"] else 0, meta.get("stargazers_count"),
            (meta.get("updated_at") or "")[:10] or None,
            license_obj.get("spdx_id") or license_obj.get("key"),
            (meta.get("created_at") or "")[:10] or None))
        for pub_id in pub_ids:
            conn.link_repository_publication(repo_id, pub_id)

        # Владелец-человек: привязать к ИТМО-персоне по полному имени.
        if not data["is_org"]:
            pid = self._names.get(normalize_person_name(details.get("name") or ""))
            if pid and len(normalize_person_name(details.get("name") or "").split()) >= 2:
                conn.set_person_github(pid, owner_obj["login"])
                conn.link_repository_person(repo_id, pid, "owner")
        for login in contributors:
            pid = conn.person_id_by_github_login(login)
            if pid:
                conn.link_repository_person(repo_id, pid, "contributor")


def harvest_repo(gh: GitHubClient, repo_url: str, pubs: list, commit_pages: int) -> list[dict]:
    """Кандидаты-логины по одному репозиторию: владелец + контрибьюторы, с их
    профилями и git-идентичностями из коммитов."""
    parsed = parse_repo_url(repo_url)
    if not parsed:
        return []
    owner, repo = parsed
    meta = gh.get(f"/repos/{owner}/{repo}")
    if not meta:
        return []
    owner_login = (meta.get("owner") or {}).get("login")
    owner_type = (meta.get("owner") or {}).get("type")
    contributors = gh.get(f"/repos/{owner}/{repo}/contributors", params={"per_page": 100}) or []

    identities: dict[str, dict] = {}
    for page in range(1, commit_pages + 1):
        commits = gh.get(f"/repos/{owner}/{repo}/commits", params={"per_page": 100, "page": page})
        if not commits:
            break
        for c in commits:
            login = (c.get("author") or {}).get("login")
            git_author = (c.get("commit") or {}).get("author") or {}
            if not login:
                continue
            slot = identities.setdefault(login, {"emails": set(), "names": set()})
            if git_author.get("email"):
                slot["emails"].add(git_author["email"])
            if git_author.get("name"):
                slot["names"].add(git_author["name"])
        if len(commits) < 100:
            break

    roles: dict[str, str] = {}
    if owner_login and owner_type == "User":
        roles[owner_login] = "repo_owner"
    for c in contributors:
        roles.setdefault(c.get("login"), "repo_contributor")

    candidates = []
    for login, source in roles.items():
        if is_bot(login):
            continue
        profile = gh.get(f"/users/{login}") or {}
        if (profile.get("type") or owner_type) not in ("User", None):
            continue                                  # организация или бот
        ident = identities.get(login, {"emails": set(), "names": set()})
        candidates.append({
            "github_login": login,
            "github_url": profile.get("html_url") or f"https://github.com/{login}",
            "user_type": profile.get("type") or owner_type,
            "source": source,
            "repo_url": repo_url,
            "publication_ids": json.dumps(pubs, ensure_ascii=False),
            "gh_name": profile.get("name"),
            "gh_email": profile.get("email"),
            "gh_company": profile.get("company"),
            "gh_location": profile.get("location"),
            "gh_bio": profile.get("bio"),
            "gh_blog": profile.get("blog") or None,
            "gh_twitter": profile.get("twitter_username"),
            "commit_emails": json.dumps(sorted(ident["emails"]), ensure_ascii=False),
            "commit_names": json.dumps(sorted(ident["names"]), ensure_ascii=False),
        })
    return candidates


class HarvestRepos(PerRecordOperation):
    """Кандидаты-аккаунты по подтверждённым авторским репозиториям.
    Единица = один репозиторий."""
    name = "harvest_repos"
    uses_external_api = True
    source = "scripts/github_harvest.py --mode repos"

    def pending(self, ctx) -> list:
        self._gh = GitHubClient()
        done = ctx.connector.harvested_repo_urls()
        return [(url, pubs) for url, pubs in ctx.connector.confirmed_repo_links()
                if url not in done]

    def fetch(self, ctx, batch):
        url, pubs = batch[0]
        return harvest_repo(self._gh, url, pubs, GITHUB_COMMIT_PAGES)

    def save(self, ctx, batch, data) -> None:
        ctx.connector.replace_github_candidates(batch[0][0], data)


class ExpandAccounts(PerRecordOperation):
    """Соцграф: от ИТМО-организаций и уже подтверждённых аккаунтов идём к их
    репозиториям и собираем новых кандидатов. Единица = один сид-аккаунт.
    Возвращает число новых репозиториев — по нему Loop понимает схождение."""
    name = "expand_accounts"
    uses_external_api = True
    source = "scripts/github_harvest.py --mode accounts"
    max_repos_per_account = 30

    def pending(self, ctx) -> list:
        self._gh = GitHubClient()
        self._done = ctx.connector.harvested_repo_urls()
        return ctx.connector.social_graph_seeds()

    def fetch(self, ctx, batch):
        login, _ = batch[0]
        urls = []
        for page in range(1, MAX_ACCOUNT_REPO_PAGES + 1):
            data = self._gh.get(f"/users/{login}/repos", params={
                "per_page": 100, "page": page, "type": "owner", "sort": "updated"})
            if not data:
                break
            urls += [r["html_url"] for r in data if not r.get("fork") and r.get("html_url")]
            if len(data) < 100 or len(urls) >= self.max_repos_per_account:
                break
        fresh = [u for u in urls[: self.max_repos_per_account] if u not in self._done]
        return [(u, harvest_repo(self._gh, u, [], GITHUB_COMMIT_PAGES)) for u in fresh]

    def save(self, ctx, batch, data) -> None:
        for repo_url, candidates in data:
            ctx.connector.replace_github_candidates(repo_url, candidates)
            self._done.add(repo_url)
        self._added = getattr(self, "_added", 0) + len(data)

    def run(self, ctx) -> int:
        self._added = 0
        super().run(ctx)
        logger.info("[%s] новых репозиториев: %d", self.name, self._added)
        return self._added      # схождение цикла считаем по новым репо, не по сидам


# --- Слой 4 сборка --------------------------------------------------------
# (EMAIL_RE и tail уже определены выше в секциях collect_emails/openreview)


def scholar_url(researcher_urls: str | None) -> str | None:
    for u in jloads(researcher_urls):
        if "scholar.google" in (u.get("url") or ""):
            return u["url"]
    return None


def gitlab_url(researcher_urls: str | None) -> str | None:
    for u in jloads(researcher_urls):
        if "gitlab.com" in (u.get("url") or "").lower():
            return u["url"]
    return None


def usable_email(e: str | None) -> str | None:
    e = (e or "").strip().lower()
    return e if e and "@" in e and "noreply" not in e else None


def pick_email(cands: list[tuple[str, str]]) -> str | None:
    """Из кандидатов (src, email) — институциональный, иначе по порядку источника."""
    inst = [e for _, e in cands if e.endswith("@itmo.ru") or e.endswith("ifmo.ru")]
    if inst:
        return inst[0]
    order = {"orcid": 0, "page": 1, "pdf": 2, "commit": 3}
    return min(cands, key=lambda x: order.get(x[0], 9))[1] if cands else None



def best_github_email(conn, login: str) -> str | None:
    for gh_email, commit_emails, gh_blog in conn.github_candidate_email_fields(login):
        cands = ([gh_email] if gh_email else []) + jloads(commit_emails)
        m = EMAIL_RE.search(gh_blog or "")   # в поле blog иногда лежит почта
        if m:
            cands.append(m.group(0))
        for e in cands:
            u = usable_email(e)
            if u:
                return u
    return None


def collect_merge_sources(conn):
    """Кандидаты по полям из всех таблиц-источников: {pid: [...]}."""
    email, github, scholar, openrev, linkedin, gitlab = {}, {}, {}, {}, {}, {}
    for pid, emails_j, gh_j, ru_j, ln in conn.merge_person_profiles():
        for e in jloads(emails_j):
            u = usable_email(e)
            if u:
                email.setdefault(pid, []).append(("orcid", u))
        urls = jloads(gh_j)
        if urls:
            github.setdefault(pid, []).append(tail(urls[0]))
        su = scholar_url(ru_j)
        if su:
            scholar.setdefault(pid, []).append(su)
        if ln:
            linkedin.setdefault(pid, []).append(ln)
        gl = gitlab_url(ru_j)
        if gl:
            gitlab.setdefault(pid, []).append(gl)
    for pid, em, source in conn.merge_collected_emails():
        u = usable_email(em)
        if u:
            email.setdefault(pid, []).append((source, u))
    for pid, login in conn.merge_github_matched():
        em = best_github_email(conn, login)
        if em:
            email.setdefault(pid, []).append(("commit", em))
    for pid, oid, ghl, gsc, ln in conn.merge_openreview():
        if oid:
            openrev.setdefault(pid, []).append(oid)
        if ghl:
            github.setdefault(pid, []).append(tail(ghl))
        if gsc:
            scholar.setdefault(pid, []).append(gsc)
        if ln:
            linkedin.setdefault(pid, []).append(ln)
    return email, github, scholar, openrev, linkedin, gitlab


class MergeProfiles(WholeSetOperation):
    """Единая точка сборки: заливает в persons_itmo ТОЛЬКО пустые поля
    (email/github/scholar/openreview/linkedin/gitlab) из всех коллекторов, не затирая
    существующее. Идёт последним. Пере-обработки нет — проходит всех каждый раз."""
    name = "merge_profiles"
    uses_external_api = False
    source = "scripts/merge_profiles.py"

    def process_all(self, ctx) -> int:
        conn = ctx.connector
        email, github, scholar, openrev, linkedin, gitlab = collect_merge_sources(conn)
        filled = 0
        for pid, e, g, s, o, ln, gl in conn.persons_merge_targets():
            updates = {}
            if not e:
                v = pick_email(email.get(pid, []))
                if v:
                    updates["email"] = v
            if not g and github.get(pid):
                updates["github"] = github[pid][0]
            if not s and scholar.get(pid):
                updates["google_scholar"] = scholar[pid][0]
            if not o and openrev.get(pid):
                updates["openreview"] = openrev[pid][0]
            if not ln and linkedin.get(pid):
                updates["linkedin"] = linkedin[pid][0]
            if not gl and gitlab.get(pid):
                updates["gitlab"] = gitlab[pid][0]
            if updates:
                conn.update_person_fields(pid, updates)
                filled += len(updates)
        return filled


# --- Слой 5 дедуп --------------------------------

class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self.parent:
            out.setdefault(self.find(node), []).append(node)
        return out

def normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'["“”‘’«»]', "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def load_variants(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def dedup_departments(conn) -> int:
    """Сливает департаменты-алиасы (по вариантам названий) в канонические."""
    depts = {did: {"name_en": name, "variants": load_variants(var)}
             for did, name, var in conn.all_departments()}
    name_to_id = {normalize(info["name_en"]): did for did, info in depts.items()}
    uf = UnionFind()
    for did in depts:
        uf.find(did)
    for did, info in depts.items():
        for variant in info["variants"]:
            other = name_to_id.get(normalize(variant))
            if other and other != did:
                uf.union(did, other)
    merged = 0
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        canonical = max(members, key=lambda d: (len(depts[d]["variants"]), d))
        for loser in (d for d in members if d != canonical):
            logger.info("Слияние департаментов: «%s» → «%s»",
                        depts[loser]["name_en"], depts[canonical]["name_en"])
            merge_department(conn, loser, canonical, depts)
            merged += 1
    return merged


def merge_department(conn, loser: str, canonical: str, depts: dict) -> None:
    canon_variants = depts[canonical]["variants"]
    existing = {normalize(v) for v in canon_variants}
    existing.add(normalize(depts[canonical]["name_en"]))
    for cand in [depts[loser]["name_en"], *depts[loser]["variants"]]:
        if normalize(cand) not in existing:
            canon_variants.append(cand)
            existing.add(normalize(cand))
    conn.update_department_variants(canonical, json.dumps(canon_variants, ensure_ascii=False))
    for pid, field in conn.persons_with_department(loser):
        ids = [x.strip() for x in field.split(";") if x.strip()]
        ids = list(dict.fromkeys(canonical if x == loser else x for x in ids))
        conn.set_person_department(pid, "; ".join(ids))
    conn.repoint_department_junctions(loser, canonical)
    conn.delete_department(loser)


def dedup_persons(conn) -> int:
    """Сливает дубли персон (один человек = разные OpenAlex-id) в каноническую запись."""
    by_name: dict[str, list[tuple]] = {}
    for pid, name_en, variants, github in conn.all_persons_for_dedup():
        by_name.setdefault(normalize(name_en), []).append((pid, name_en, variants, github))
    merged = 0
    for norm_name, rows in by_name.items():
        if not norm_name or len(rows) < 2:
            continue
        canonical = max(rows, key=lambda r: (bool(r[3]), len(load_variants(r[2])), r[0]))
        canon_id = canonical[0]
        for loser in (r for r in rows if r[0] != canon_id):
            logger.info("Слияние персон «%s»: %s → %s", canonical[1], loser[0], canon_id)
            merge_person(conn, loser[0], canon_id)
            merged += 1
    return merged


def merge_person(conn, loser: str, canonical: str) -> None:
    conn.repoint_person_junctions(loser, canonical)
    lo = conn.person_variants_github(loser)
    ca = conn.person_variants_github(canonical)
    if lo and ca:
        merged_variants = list(dict.fromkeys(load_variants(ca[0]) + load_variants(lo[0])))
        conn.update_person_merged(canonical, json.dumps(merged_variants, ensure_ascii=False),
                                  ca[1] or lo[1])
    conn.delete_person(loser)


class DedupFinalize(WholeSetOperation):
    """Дедуп персон и департаментов (по всему набору), последним. Репо-склейки
    чинятся в extract_repo_links; пересчёт производных — в connector.rebuild_derived."""
    name = "finalize_dedup"
    uses_external_api = False
    source = "scripts/finalize.py (dedup_departments + dedup_persons)"

    def process_all(self, ctx) -> int:
        d = dedup_departments(ctx.connector)
        p = dedup_persons(ctx.connector)
        logger.info("[%s] слито департаментов: %d, персон: %d", self.name, d, p)
        return d + p