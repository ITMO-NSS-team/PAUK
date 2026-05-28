"""Извлекает кандидатные ссылки на репозитории кода из материала публикаций.

Источников два:

1. **Полный текст PDF** (если он скачан в data/pdfs/). Берётся через PyMuPDF
   страница за страницей, плюс отдельно page.get_links() — это URL,
   зашитые в кликабельные гиперссылки PDF (/Annots). Гиперссылки часто
   ловят URL, которые в видимом тексте не написаны словом «here».
2. **Абстракт из OpenAlex**, если у публикации нет скачанного PDF, но
   абстракт мы сохранили (см. scripts/fetch_papers.py).

В обоих случаях текст нормализуется (склейка переносов внутри URL), затем
прогоняется регулярка по списку SUPPORTED_HOSTS. Результат сохраняется
в таблицу repo_links.

Запускать из корня проекта:
    uv run python scripts/extract_repo_links.py
"""

import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import fitz
from config import (
    CONTEXT_RADIUS,
    DB_PATH,
    SUPPORTED_HOSTS,
    URL_TRAILING_PUNCT,
    pdf_path_for,
)

# (?<![\w.]) — перед хостом не должно быть буквы/цифры/точки, иначе
# "notgithub.com/foo" совпадёт с середины. В негативный класс path добавлены
# < и >, чтобы не съедать LaTeX-разметку вида "<Name>".
URL_PATTERN = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.)?("
    + "|".join(re.escape(host) for host in SUPPORTED_HOSTS)
    + r')(/[^\s<>)\]}"\']+)',
    re.IGNORECASE,
)

# Дефис-перенос длинного слова в конце строки: "hugging-\nface" -> "huggingface".
HYPHEN_LINEBREAK = re.compile(r"(?<=\w)-\n\s*(?=\w)")
# Перенос строки внутри URL без дефиса: "huggingface.co/dat\nasets/..." -> "huggingface.co/datasets/...".
URL_LINEBREAK = re.compile(r"(?<=[\w/.\-?&=#%])\n\s*(?=[\w/])")


SENTENCE_START_TAIL = re.compile(r"\.(?:[A-Z][a-z]+[\w-]*|\d+(?:\.\d+)*)$")

# Хосты, у которых каноническая ссылка на репо — host/user/repo.
# Всё, что после второго сегмента пути (`/pull/69`, `/tree/main`,
# `/blob/...`, `/issues/...`), это навигация внутри одного и того же
# репозитория и должна сворачиваться в корень при дедупликации.
REPO_ROOT_HOSTS = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "gitee.com",
    "huggingface.co",
}


def split_at_embedded_url(url: str) -> str:
    """Отрезает второй вложенный http(s):// в URL (типичная склейка
    ссылки и сноски в PDF, вроде ``...repo3https://github.com/...``)."""
    positions = [
        pos for pos in (url.find("http://", 1), url.find("https://", 1)) if pos > 0
    ]
    if positions:
        return url[: min(positions)]
    return url


def canonicalize_repo_url(url: str) -> str:
    """Для github/gitlab/bitbucket/codeberg/gitee/huggingface сворачивает URL
    к ``https://host/user/repo``. Отрезает /pull/N, /tree/branch, /blob/...,
    /issues/N, а также хвостовой .git. Для остальных хостов возвращает URL
    без изменений (Zenodo, Figshare, OSF имеют другие схемы путей)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = parsed.netloc.lower()
    bare_host = host[4:] if host.startswith("www.") else host
    if bare_host not in REPO_ROOT_HOSTS:
        return url
    segments = [seg for seg in parsed.path.split("/") if seg]
    if len(segments) < 2:
        return url
    repo = segments[1].removesuffix(".git")
    return f"https://{bare_host}/{segments[0]}/{repo}"


def clean_url_tail(url: str) -> str:
    """Канонизирует найденный URL:

    1. Срезает хвостовую пунктуацию.
    2. Срезает прилипшее начало следующего предложения (``.Word``, ``.4.1``).
    3. Разрезает склеенные через сноску ссылки (``repo3https://...``).
    4. Добавляет схему https:// если её нет.
    5. Сворачивает GitHub/HF/GitLab и т.п. до корня репо (host/user/repo).
    """
    url = SENTENCE_START_TAIL.sub("", url.rstrip(URL_TRAILING_PUNCT))
    url = split_at_embedded_url(url)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = canonicalize_repo_url(url)
    return url


# Источник кандидата — в логах и для диагностики.
SOURCE_TEXT = "pdf_text"
SOURCE_ANNOT = "pdf_annotation"
SOURCE_ABSTRACT = "abstract"


def normalize_pdf_text(text: str) -> str:
    """Склеивает дефис-переносы слов и разрывы строк внутри URL-подобных кусков."""
    text = HYPHEN_LINEBREAK.sub("", text)
    text = URL_LINEBREAK.sub("", text)
    return text


def host_matches(url: str) -> str | None:
    """Если netloc URL равен поддерживаемому хосту (или его поддомену), возвращает хост.

    Использует urlparse, а не подстрочный поиск, чтобы 'notgithub.com'
    не давал false-positive по 'github.com'.
    """
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
    netloc = parsed.netloc.lower()
    if not netloc:
        return None
    for host in SUPPORTED_HOSTS:
        if netloc == host or netloc.endswith("." + host):
            return host
    return None


def find_urls_in_text(text: str) -> list[tuple[str, str, int, int]]:
    """Прогоняет URL_PATTERN по тексту. Возвращает (url, host, start, end)."""
    found: list[tuple[str, str, int, int]] = []
    for match in URL_PATTERN.finditer(text):
        host = match.group(1).lower()
        url = clean_url_tail(match.group(0))
        found.append((url, host, match.start(), match.end()))
    return found


def slice_context(text: str, start: int, end: int) -> str:
    """Возвращает кусок текста вокруг позиции [start, end] длиной ±CONTEXT_RADIUS."""
    ctx_start = max(0, start - CONTEXT_RADIUS)
    ctx_end = min(len(text), end + CONTEXT_RADIUS)
    return text[ctx_start:ctx_end]


def extract_from_pdf_page(page: fitz.Page) -> list[tuple[str, str, str, str]]:
    """Возвращает список (url, host, context, source) для одной страницы PDF.

    Сканируется и видимый текст, и `/Annots` (кликабельные гиперссылки).
    """
    results: list[tuple[str, str, str, str]] = []

    raw_text = page.get_text()
    normalized = normalize_pdf_text(raw_text) if raw_text else ""

    # 1. Видимый текст
    for url, host, start, end in find_urls_in_text(normalized):
        results.append((url, host, slice_context(normalized, start, end), SOURCE_TEXT))

    # 2. Гиперссылки-аннотации
    for link in page.get_links() or []:
        uri = link.get("uri")
        if not uri or link.get("kind") != fitz.LINK_URI:
            continue
        host = host_matches(uri)
        if not host:
            continue
        url = clean_url_tail(uri)

        # Контекст: видимый текст под прямоугольником ссылки + окружение в
        # полном тексте страницы. Если ничего не нашлось — берём сам URL.
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

        results.append((url, host, context, SOURCE_ANNOT))

    return results


def extract_from_pdf(pdf_path: Path) -> list[tuple[str, str, str, int | None, str]]:
    """Возвращает (url, host, context, page_number, source) по всему PDF."""
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        print(f"  не удалось открыть PDF: {exc}")
        return []

    results: list[tuple[str, str, str, int | None, str]] = []
    for page_num, page in enumerate(doc, 1):
        for url, host, context, source in extract_from_pdf_page(page):
            results.append((url, host, context, page_num, source))
    doc.close()
    return results


def extract_from_abstract(
    abstract: str,
) -> list[tuple[str, str, str, int | None, str]]:
    """Возвращает (url, host, context, None, 'abstract') по тексту абстракта."""
    if not abstract:
        return []
    normalized = normalize_pdf_text(abstract)
    results: list[tuple[str, str, str, int | None, str]] = []
    for url, host, start, end in find_urls_in_text(normalized):
        results.append(
            (url, host, slice_context(normalized, start, end), None, SOURCE_ABSTRACT)
        )
    return results


def deduplicate(
    links: list[tuple[str, str, str, int | None, str]],
) -> list[tuple[str, str, str, int | None, str]]:
    """Оставляет только одну запись на URL внутри одной публикации.

    Приоритет источников: pdf_text > pdf_annotation > abstract - у текста
    наиболее полезный окружающий контекст для LLM, у аннотации - обычно
    лишь подпись «here»/«link», а абстракт даёт меньше всего.
    """
    priority = {SOURCE_TEXT: 0, SOURCE_ANNOT: 1, SOURCE_ABSTRACT: 2}
    chosen: dict[str, tuple[str, str, str, int | None, str]] = {}
    for entry in links:
        url = entry[0]
        if url not in chosen or priority[entry[4]] < priority[chosen[url][4]]:
            chosen[url] = entry
    return list(chosen.values())


def fetch_processable_publications(
    conn: sqlite3.Connection,
) -> list[tuple[str, str | None]]:
    """Возвращает (id, abstract) для публикаций, по которым есть что парсить.

    Сейчас критерий — pdf_url или abstract не пустые. Наличие самого PDF
    проверяется отдельно через pdf_path_for(id).exists() в main, потому что
    локальный путь в БД не хранится.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, abstract
        FROM publications
        WHERE (pdf_url IS NOT NULL AND pdf_url != '')
           OR (abstract IS NOT NULL AND abstract != '')
        """
    )
    return cur.fetchall()


def save_links(
    conn: sqlite3.Connection,
    publication_id: str,
    links: list[tuple[str, str, str, int | None, str]],
) -> None:
    """Полностью заменяет строки repo_links для одной публикации новым набором.

    Поле source в БД пока не хранится - оно полезно только для логов
    текущего запуска. Если позже понадобится, добавим колонку.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM repo_links WHERE publication_id = ?", (publication_id,))
    cur.executemany(
        """
        INSERT INTO repo_links (publication_id, url, host, context, page_number)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (publication_id, url, host, context, page)
            for url, host, context, page, _ in links
        ],
    )
    conn.commit()


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = fetch_processable_publications(conn)
        if not rows:
            print(
                "Нет публикаций с PDF или абстрактом. Сначала запусти scripts/fetch_papers.py."
            )
            return

        print(f"Обрабатываю {len(rows)} публикаций")
        total_links = 0
        pubs_with_links = 0
        per_source = {SOURCE_TEXT: 0, SOURCE_ANNOT: 0, SOURCE_ABSTRACT: 0}

        for index, (pub_id, abstract) in enumerate(rows, 1):
            print(f"[{index}/{len(rows)}] {pub_id}")

            links: list[tuple[str, str, str, int | None, str]] = []
            pdf_path = pdf_path_for(pub_id)
            if pdf_path.exists():
                links = extract_from_pdf(pdf_path)
            if not links and abstract:
                links = extract_from_abstract(abstract)

            links = deduplicate(links)
            save_links(conn, pub_id, links)

            if not links:
                print("  ссылок не найдено")
                continue

            pubs_with_links += 1
            total_links += len(links)
            for url, host, _, page, source in links:
                per_source[source] += 1
                page_label = f"стр.{page}" if page is not None else "абстракт"
                print(f"  [{host} {page_label} | {source}] {url}")

        print()
        print(f"Обработано публикаций:        {len(rows)}")
        print(f"С найденными ссылками:        {pubs_with_links}")
        print(f"Всего ссылок сохранено:       {total_links}")
        print(f"  из видимого текста PDF:     {per_source[SOURCE_TEXT]}")
        print(f"  из аннотаций PDF:           {per_source[SOURCE_ANNOT]}")
        print(f"  из абстрактов:              {per_source[SOURCE_ABSTRACT]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
