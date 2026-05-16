"""Извлекает кандидатные ссылки на репозитории кода из скачанных PDF.

Для каждой публикации, у которой есть локально сохранённый PDF, скрипт:

1. Читает текст каждой страницы через PyMuPDF.
2. Склеивает разрывы строк, чтобы не терять URL, разорванные переносом.
3. Ищет ссылки, чей хост входит в SUPPORTED_HOSTS из config.py.
4. Сохраняет каждое совпадение вместе с окружающим контекстом в таблицу
   repo_links.

Запускать из корня проекта:
    uv run python scripts/extract_repo_links.py
"""

import re
import sqlite3
from pathlib import Path

import fitz
from config import (
    CONTEXT_RADIUS,
    DB_PATH,
    SUPPORTED_HOSTS,
    URL_TRAILING_PUNCT,
)

URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?("
    + "|".join(re.escape(host) for host in SUPPORTED_HOSTS)
    + r')(/[^\s)\]}>"\']+)',
    re.IGNORECASE,
)

# Дефис-перенос длинного слова в конце строки: "hugging-\nface" -> "huggingface".
HYPHEN_LINEBREAK = re.compile(r"(?<=\w)-\n\s*(?=\w)")
# Перенос строки внутри URL без дефиса: "huggingface.co/dat\nasets/..." -> "huggingface.co/datasets/...".
URL_LINEBREAK = re.compile(r"(?<=[\w/.\-?&=#%])\n\s*(?=[\w/])")


def normalize_pdf_text(text: str) -> str:
    """Склеивает дефис-переносы слов и разрывы строк внутри URL-подобных кусков.

    PyMuPDF возвращает текст с теми же переносами строк, что использует PDF
    при рендеринге. Длинные URL почти всегда разбиваются переносом, и без
    этой нормализации регулярка ``URL_PATTERN`` молча обрезает их.
    """
    text = HYPHEN_LINEBREAK.sub("", text)
    text = URL_LINEBREAK.sub("", text)
    return text


def extract_from_pdf(pdf_path: Path) -> list[tuple[str, str, str, int]]:
    """Возвращает все кандидатные URL, найденные в PDF.

    Каждый кортеж — ``(url, host, context, page_number)``, где ``context`` —
    до ``CONTEXT_RADIUS`` символов с каждой стороны URL из того же
    нормализованного текста страницы.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        print(f"  не удалось открыть PDF: {exc}")
        return []

    results: list[tuple[str, str, str, int]] = []
    for page_num, page in enumerate(doc, 1):
        text = page.get_text()
        if not text:
            continue
        text = normalize_pdf_text(text)
        for match in URL_PATTERN.finditer(text):
            host = match.group(1).lower()
            url = match.group(0).rstrip(URL_TRAILING_PUNCT)
            start, end = match.span()
            ctx_start = max(0, start - CONTEXT_RADIUS)
            ctx_end = min(len(text), end + CONTEXT_RADIUS)
            context = text[ctx_start:ctx_end]
            results.append((url, host, context, page_num))
    doc.close()
    return results


def fetch_publications_with_pdfs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Возвращает [(publication_id, pdf_local_path), ...] для всех публикаций с PDF."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, pdf_local_path
        FROM publications
        WHERE pdf_local_path IS NOT NULL AND pdf_local_path != ''
        """
    )
    return cur.fetchall()


def save_links(
    conn: sqlite3.Connection,
    publication_id: str,
    links: list[tuple[str, str, str, int]],
) -> None:
    """Полностью заменяет строки repo_links для одной публикации новым набором."""
    cur = conn.cursor()
    cur.execute("DELETE FROM repo_links WHERE publication_id = ?", (publication_id,))
    cur.executemany(
        """
        INSERT INTO repo_links (publication_id, url, host, context, page_number)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (publication_id, url, host, context, page)
            for url, host, context, page in links
        ],
    )
    conn.commit()


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = fetch_publications_with_pdfs(conn)
        if not rows:
            print(
                "Нет ни одного скачанного PDF. Сначала запусти scripts/fetch_papers.py."
            )
            return

        print(f"Обрабатываю {len(rows)} PDF")
        total_links = 0
        pubs_with_links = 0

        for index, (pub_id, pdf_path_str) in enumerate(rows, 1):
            pdf_path = Path(pdf_path_str)
            print(f"[{index}/{len(rows)}] {pub_id}")
            if not pdf_path.exists():
                print(f"  файл не найден: {pdf_path}")
                continue

            links = extract_from_pdf(pdf_path)
            save_links(conn, pub_id, links)

            if not links:
                print("  ссылок не найдено")
                continue

            pubs_with_links += 1
            total_links += len(links)
            for url, host, _, page in links:
                print(f"  [{host} стр.{page}] {url}")

        print()
        print(f"Обработано PDF:           {len(rows)}")
        print(f"С найденными ссылками:    {pubs_with_links}")
        print(f"Всего ссылок сохранено:   {total_links}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
