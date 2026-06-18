import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

import fitz
import requests
from config import (
    BROWSER_USER_AGENT,
    CLASSIFY_MODEL,
    CONTEXT_RADIUS,
    DB_PATH,
    DOWNLOAD_TIMEOUT,
    OPENALEX_API_KEY,
    OPENALEX_WORKS_URL,
    PDF_DIR,
    REQUEST_DELAY,
    USER_AGENT,
    pdf_path_for,
)
from llm import chat_json

PDF_MAGIC = b"%PDF"
URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"

SOURCE_TEXT = "pdf_text"
SOURCE_ANNOT = "pdf_annotation"
SOURCE_ABSTRACT = "abstract"


# --- (abstract + PDF) -------------------------------------------------------------

def openalex_params() -> dict:
    return {"api_key": OPENALEX_API_KEY} if OPENALEX_API_KEY else {}


def reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Склеивает абстракт из формата OpenAlex {слово: [позиции]}."""
    if not inverted_index:
        return None
    positions = [(idx, word) for word, idxs in inverted_index.items() for idx in idxs]
    positions.sort()
    return " ".join(word for _, word in positions)


def fetch_paper_data(session: requests.Session, openalex_id: str) -> dict | None:
    try:
        response = session.get(
            f"{OPENALEX_WORKS_URL}/{openalex_id}",
            params=openalex_params(),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"  ошибка запроса к OpenAlex: {exc}")
        return None
    if response.status_code != 200:
        print(f"  OpenAlex вернул HTTP {response.status_code}")
        return None
    data = response.json()
    pdf_url = None
    for source in (data.get("best_oa_location"), data.get("primary_location")):
        if source and source.get("pdf_url"):
            pdf_url = source["pdf_url"]
            break
    if not pdf_url:
        for location in data.get("locations") or []:
            if location.get("pdf_url"):
                pdf_url = location["pdf_url"]
                break
    return {"pdf_url": pdf_url, "abstract": reconstruct_abstract(data.get("abstract_inverted_index"))}


def download_pdf(session: requests.Session, pdf_url: str, dest: Path) -> bool:
    """Качает pdf_url в dest. True только если файл реально PDF."""
    try:
        response = session.get(pdf_url, timeout=DOWNLOAD_TIMEOUT, stream=True, allow_redirects=True)
    except requests.RequestException as exc:
        print(f"  ошибка скачивания: {exc}")
        return False
    if response.status_code != 200:
        print(f"  HTTP {response.status_code} при скачивании")
        return False
    try:
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    handle.write(chunk)
    except requests.RequestException as exc:
        print(f"  поток оборвался: {exc}")
        dest.unlink(missing_ok=True)
        return False
    with dest.open("rb") as handle:
        if handle.read(4) != PDF_MAGIC:
            print(f"  файл не PDF (Content-Type: {response.headers.get('Content-Type', '?')})")
            dest.unlink(missing_ok=True)
            return False
    return True


def run_fetch(conn: sqlite3.Connection) -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    api_session = requests.Session()
    api_session.headers.update({"User-Agent": USER_AGENT})
    browser_session = requests.Session()
    browser_session.headers.update({"User-Agent": BROWSER_USER_AGENT})

    cur = conn.cursor()
    pubs = cur.execute(
        "SELECT id, pdf_url, abstract FROM publications WHERE pdf_url IS NULL OR abstract IS NULL"
    ).fetchall()
    if not pubs:
        print("Материал уже собран по всем публикациям.")
        return

    print(f"[fetch] обрабатываю {len(pubs)} публикаций")
    downloaded = 0
    for index, (pub_id, current_pdf_url, current_abstract) in enumerate(pubs, 1):
        print(f"[fetch {index}/{len(pubs)}] {pub_id}")
        data = fetch_paper_data(api_session, pub_id)
        if data is None:
            time.sleep(REQUEST_DELAY)
            continue
        # Пустая строка = «проверено, ничего нет» — чтобы не дёргать снова.
        if current_abstract is None:
            conn.execute("UPDATE publications SET abstract = ? WHERE id = ?", (data["abstract"] or "", pub_id))
        if current_pdf_url is None:
            conn.execute("UPDATE publications SET pdf_url = ? WHERE id = ?", (data["pdf_url"] or "", pub_id))
            dest = pdf_path_for(pub_id)
            if data["pdf_url"] and not dest.exists() and download_pdf(browser_session, data["pdf_url"], dest):
                downloaded += 1
                print(f"  PDF скачан ({dest.stat().st_size // 1024} КБ)")
        conn.commit()
        time.sleep(REQUEST_DELAY)
    print(f"[fetch] PDF скачано: {downloaded}")



# --- извлечение ссылок на github.com -------------------------------------------------------------

URL_PATTERN = re.compile(
    r'(?<![\w.])(?:https?://)?(?:www\.)?(github\.com)(/[^\s<>)\]}"\']+)', re.IGNORECASE
)
HYPHEN_LINEBREAK = re.compile(r"(?<=\w)-\n\s*(?=\w)")
URL_LINEBREAK = re.compile(r"(?<=[\w/.\-?&=#%])\n\s*(?=[\w/])")
SENTENCE_START_TAIL = re.compile(r"\.(?:[A-Z][a-z]+[\w-]*|[A-Z]{2,}[\w-]*|\d+(?:\.\d+)*)$")


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
    return text[max(0, start - CONTEXT_RADIUS) : min(len(text), end + CONTEXT_RADIUS)]


def extract_from_pdf_page(page: fitz.Page) -> list[tuple[str, str, str]]:
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


def extract_from_pdf(pdf_path: Path) -> list[tuple[str, str, int | None, str]]:
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        print(f"  не удалось открыть PDF: {exc}")
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


def deduplicate(links: list[tuple[str, str, int | None, str]]) -> list[tuple[str, str, int | None, str]]:
    """Одна запись на URL. Приоритет источника: текст PDF > аннотация > абстракт."""
    priority = {SOURCE_TEXT: 0, SOURCE_ANNOT: 1, SOURCE_ABSTRACT: 2}
    chosen: dict[str, tuple[str, str, int | None, str]] = {}
    for entry in links:
        url = entry[0]
        if url not in chosen or priority[entry[3]] < priority[chosen[url][3]]:
            chosen[url] = entry
    return list(chosen.values())


def run_extract(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, abstract FROM publications
        WHERE (pdf_url IS NOT NULL AND pdf_url != '')
           OR (abstract IS NOT NULL AND abstract != '')
        """
    ).fetchall()
    if not rows:
        print("[extract] нет публикаций с материалом.")
        return

    print(f"[extract] обрабатываю {len(rows)} публикаций")
    total = 0
    for index, (pub_id, abstract) in enumerate(rows, 1):
        links = []
        pdf_path = pdf_path_for(pub_id)
        if pdf_path.exists():
            links = extract_from_pdf(pdf_path)
        if not links and abstract:
            links = extract_from_abstract(abstract)
        links = deduplicate(links)

        cur.execute("DELETE FROM repo_links WHERE publication_id = ?", (pub_id,))
        cur.executemany(
            "INSERT INTO repo_links (publication_id, url, host, context, page_number) VALUES (?, ?, 'github.com', ?, ?)",
            [(pub_id, url, context, page) for url, context, page, _ in links],
        )
        conn.commit()
        total += len(links)
        if links:
            print(f"[extract {index}/{len(rows)}] {pub_id}: {len(links)} ссылок")
    print(f"[extract] всего ссылок: {total}")



# --- классификация ссылок (LLM) -------------------------------------------------------------

PROMPT_TEMPLATE = """Ты помогаешь анализировать научные публикации.

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


def build_prompt(title, authors, url, context, page_number) -> str:
    hint = (
        f"Источник: видимый текст PDF, страница {page_number}."
        if page_number is not None
        else "Источник: абстракт из OpenAlex (контекст ограничен)."
    )
    return PROMPT_TEMPLATE.format(
        title=title or "(без названия)",
        authors=authors or "(авторы не указаны)",
        url=url,
        source_hint=hint,
        context=context or "(контекст пустой)",
    )


def run_classify(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT rl.id, rl.publication_id, rl.url, rl.context, rl.page_number, p.title, p.authors
        FROM repo_links rl JOIN publications p ON p.id = rl.publication_id
        WHERE rl.is_relevant IS NULL
        ORDER BY rl.id
        """
    ).fetchall()
    if not rows:
        print("[classify] нет неклассифицированных ссылок.")
        return

    print(f"[classify] {len(rows)} ссылок через {CLASSIFY_MODEL}")
    yes = no = failed = 0
    for index, (link_id, pub_id, url, context, page, title, authors) in enumerate(rows, 1):
        prompt = build_prompt(title, authors, url, context, page)
        result = chat_json(CLASSIFY_MODEL, [{"role": "user", "content": prompt}])
        if result is None:
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue
        is_relevant = bool(result.get("is_authors_artifact"))
        try:
            confidence = float(result.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(result.get("reason") or "").strip()
        cur.execute(
            "UPDATE repo_links SET is_relevant = ?, llm_confidence = ?, llm_reason = ? WHERE id = ?",
            (1 if is_relevant else 0, confidence, reason, link_id),
        )
        conn.commit()
        yes += is_relevant
        no += not is_relevant
        print(f"[classify {index}/{len(rows)}] {url[:60]} -> {'ДА' if is_relevant else 'нет'}")
        time.sleep(REQUEST_DELAY)
    print(f"[classify] авторских: {yes}, чужих: {no}, не удалось: {failed}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        run_fetch(conn)
        run_extract(conn)
        run_classify(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
