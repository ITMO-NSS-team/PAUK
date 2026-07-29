"""Извлечение github ссылок из PDF/абстрактов, канонизация URL, промпт классификации."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import fitz

from ..config import CONTEXT_RADIUS

logger = logging.getLogger(__name__)


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

    Схлопывание идёт по каноническому URL, поэтому /tree/main, .git и склеенные
    хвосты не дают дублей репозиториев.
    """
    chosen: dict[str, tuple[str, str, int | None, str]] = {}
    for entry in links:
        url = entry[0]
        if url not in chosen or SOURCE_PRIORITY[entry[3]] < SOURCE_PRIORITY[chosen[url][3]]:
            chosen[url] = entry
    return [(url, context, page) for url, context, page, _ in chosen.values()]

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
