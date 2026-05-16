"""Скачивает open-access PDF для публикаций, уже сохранённых в БД.

Для каждой публикации, которую мы ещё не проверяли, скрипт:

1. Делает запрос к OpenAlex /works.
2. Ищет прямой URL PDF в полях best_oa_location, primary_location и
   во всех остальных locations.
3. Записывает найденный URL в publications.pdf_url - либо пустую строку,
   если у работы вообще нет открытого источника.
4. Скачивает PDF в data/pdfs/{publication_id}.pdf и сохраняет путь в
   publications.pdf_local_path.

Запускать из корня проекта:
    uv run python scripts/fetch_papers.py --limit 10
"""

import argparse
import sqlite3
import time
from pathlib import Path

import requests
from config import (
    DB_PATH,
    DOWNLOAD_TIMEOUT,
    FETCH_BATCH_SIZE,
    OPENALEX_API_KEY,
    OPENALEX_WORKS_URL,
    PDF_DIR,
    REQUEST_DELAY,
    USER_AGENT,
)

PDF_MAGIC = b"%PDF"


def make_session() -> requests.Session:
    """Создаёт requests-сессию с настроенным User-Agent для polite pool OpenAlex."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def openalex_params() -> dict:
    """Параметры запроса с API-ключом, если он указан в config.py."""
    if OPENALEX_API_KEY and OPENALEX_API_KEY != "REPLACE_ME":
        return {"api_key": OPENALEX_API_KEY}
    return {}


def fetch_pdf_url(session: requests.Session, openalex_id: str) -> str | None:
    """Возвращает прямую ссылку на PDF для работы или None, если OA-источника нет."""
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
    for source in (data.get("best_oa_location"), data.get("primary_location")):
        if source and source.get("pdf_url"):
            return source["pdf_url"]
    for location in data.get("locations") or []:
        if location.get("pdf_url"):
            return location["pdf_url"]
    return None


def download_pdf(session: requests.Session, pdf_url: str, dest: Path) -> bool:
    """Качает ``pdf_url`` в ``dest``. Возвращает True, если файл действительно PDF."""
    try:
        response = session.get(
            pdf_url, timeout=DOWNLOAD_TIMEOUT, stream=True, allow_redirects=True
        )
    except requests.RequestException as exc:
        print(f"  ошибка скачивания: {exc}")
        return False

    if response.status_code != 200:
        print(f"  HTTP {response.status_code} при скачивании")
        return False

    with dest.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                handle.write(chunk)

    with dest.open("rb") as handle:
        if handle.read(4) != PDF_MAGIC:
            content_type = response.headers.get("Content-Type", "?")
            print(f"  скачанный файл не PDF (Content-Type: {content_type})")
            dest.unlink(missing_ok=True)
            return False
    return True


def fetch_unchecked_publications(
    conn: sqlite3.Connection, limit: int
) -> list[tuple[str]]:
    """Возвращает id публикаций, для которых ещё не пытались доставать PDF."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM publications WHERE pdf_url IS NULL LIMIT ?", (limit,))
    return cur.fetchall()


def set_pdf_url(conn: sqlite3.Connection, publication_id: str, value: str) -> None:
    """Сохраняет найденный URL PDF (или пустую строку как маркер «нет OA»)."""
    conn.execute(
        "UPDATE publications SET pdf_url = ? WHERE id = ?", (value, publication_id)
    )
    conn.commit()


def set_pdf_local_path(
    conn: sqlite3.Connection, publication_id: str, path: Path
) -> None:
    """Сохраняет путь до локально скачанного PDF."""
    conn.execute(
        "UPDATE publications SET pdf_local_path = ? WHERE id = ?",
        (str(path), publication_id),
    )
    conn.commit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Качает open-access PDF для публикаций из БД через OpenAlex."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=FETCH_BATCH_SIZE,
        help=f"Сколько необработанных публикаций взять за один запуск (по умолчанию: {FETCH_BATCH_SIZE}).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    session = make_session()
    try:
        pubs = fetch_unchecked_publications(conn, args.limit)
        if not pubs:
            print("Все публикации уже хотя бы раз проверены — обрабатывать нечего.")
            return

        print(f"Обрабатываю {len(pubs)} публикаций (лимит {args.limit})")
        stats = {"checked": 0, "downloaded": 0, "already": 0, "no_oa": 0, "failed": 0}

        for index, (pub_id,) in enumerate(pubs, 1):
            print(f"[{index}/{len(pubs)}] {pub_id}")
            stats["checked"] += 1

            pdf_url = fetch_pdf_url(session, pub_id)
            if not pdf_url:
                print("  open-access PDF не указан")
                set_pdf_url(conn, pub_id, "")
                stats["no_oa"] += 1
                time.sleep(REQUEST_DELAY)
                continue

            print(f"  pdf_url: {pdf_url}")
            set_pdf_url(conn, pub_id, pdf_url)

            dest = PDF_DIR / f"{pub_id}.pdf"
            if dest.exists():
                print("  файл уже скачан, обновляю путь в БД")
                set_pdf_local_path(conn, pub_id, dest)
                stats["already"] += 1
                time.sleep(REQUEST_DELAY)
                continue

            if download_pdf(session, pdf_url, dest):
                size_kb = dest.stat().st_size // 1024
                print(f"  скачан ({size_kb} КБ)")
                set_pdf_local_path(conn, pub_id, dest)
                stats["downloaded"] += 1
            else:
                stats["failed"] += 1

            time.sleep(REQUEST_DELAY)

        print()
        print(f"Проверено:        {stats['checked']}")
        print(f"Скачано:          {stats['downloaded']}")
        print(f"Уже было локально:{stats['already']}")
        print(f"Без OA-источника: {stats['no_oa']}")
        print(f"Ошибок скачивания:{stats['failed']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
