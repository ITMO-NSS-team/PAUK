"""Обогащает публикации в БД материалом для последующего поиска ссылок.

Для каждой публикации, у которой ещё не заполнены поля pdf_url и/или
abstract, скрипт делает один запрос к OpenAlex /works и:

1. Ищет прямой URL PDF в best_oa_location, primary_location и
   остальных locations. Если PDF указан - сохраняет URL и скачивает
   файл в data/pdfs/{publication_id}.pdf.
2. Реконструирует абстракт из abstract_inverted_index (OpenAlex хранит
   абстракт в виде «слово -> список позиций») и сохраняет его в
   publications.abstract.

Пустые строки в pdf_url и abstract используются как маркер
«проверено, OpenAlex ничего не отдал». Это нужно, чтобы при повторном
запуске не дёргать API за уже проверенными публикациями.

Параметр --limit задаёт, сколько необработанных публикаций взять за
один запуск.

Запускать из корня проекта:
    uv run python scripts/fetch_papers.py --limit 10
"""

import argparse
import sqlite3
import time
from pathlib import Path

import requests
from config import (
    BROWSER_USER_AGENT,
    DB_PATH,
    DOWNLOAD_TIMEOUT,
    FETCH_BATCH_SIZE,
    OPENALEX_API_KEY,
    OPENALEX_WORKS_URL,
    PDF_DIR,
    REQUEST_DELAY,
    USER_AGENT,
    pdf_path_for,
)

PDF_MAGIC = b"%PDF"


def make_api_session() -> requests.Session:
    """Сессия с UA `ITMO-Research-Monitor/...` — для запросов к OpenAlex API."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def make_browser_session() -> requests.Session:
    """Сессия с браузерным UA — для скачивания PDF с сайтов издателей."""
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_USER_AGENT})
    return session


def openalex_params() -> dict:
    """Параметры запроса с API-ключом, если он задан в .env."""
    if OPENALEX_API_KEY:
        return {"api_key": OPENALEX_API_KEY}
    return {}


def reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Восстанавливает текст абстракта из формата OpenAlex (слово -> позиции).

    OpenAlex по юридическим причинам не отдаёт абстракт сплошным текстом, а
    хранит его как ``{слово: [позиция_1, позиция_2, ...]}``. Здесь мы
    сортируем по позициям и склеиваем обратно.
    """
    if not inverted_index:
        return None
    positions: list[tuple[int, str]] = []
    for word, indices in inverted_index.items():
        for idx in indices:
            positions.append((idx, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def fetch_paper_data(session: requests.Session, openalex_id: str) -> dict | None:
    """Возвращает {'pdf_url': str|None, 'abstract': str|None} из OpenAlex /works."""
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

    pdf_url: str | None = None
    for source in (data.get("best_oa_location"), data.get("primary_location")):
        if source and source.get("pdf_url"):
            pdf_url = source["pdf_url"]
            break
    if not pdf_url:
        for location in data.get("locations") or []:
            if location.get("pdf_url"):
                pdf_url = location["pdf_url"]
                break

    abstract = reconstruct_abstract(data.get("abstract_inverted_index"))
    return {"pdf_url": pdf_url, "abstract": abstract}


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
        header = handle.read(4)
    if header != PDF_MAGIC:
        content_type = response.headers.get("Content-Type", "?")
        print(f"  скачанный файл не PDF (Content-Type: {content_type})")
        dest.unlink(missing_ok=True)
        return False
    return True


def fetch_unenriched_publications(
    conn: sqlite3.Connection, limit: int
) -> list[tuple[str, str | None, str | None]]:
    """Возвращает (id, pdf_url, abstract) для публикаций, у которых хотя бы одно поле NULL."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, pdf_url, abstract
        FROM publications
        WHERE pdf_url IS NULL OR abstract IS NULL
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


def set_pdf_url(conn: sqlite3.Connection, publication_id: str, value: str) -> None:
    """Сохраняет найденный URL PDF (или пустую строку как маркер «нет OA»)."""
    conn.execute(
        "UPDATE publications SET pdf_url = ? WHERE id = ?", (value, publication_id)
    )
    conn.commit()


def set_abstract(conn: sqlite3.Connection, publication_id: str, value: str) -> None:
    """Сохраняет реконструированный абстракт (или пустую строку как маркер «нет»)."""
    conn.execute(
        "UPDATE publications SET abstract = ? WHERE id = ?", (value, publication_id)
    )
    conn.commit()


def fetch_failed_pdf_publications(
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Публикации, у которых pdf_url есть, но файл так и не лежит локально.

    Проверка существования файла идёт в Python — мы больше не храним
    pdf_local_path в БД (путь детерминирован: data/pdfs/{id}.pdf).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, pdf_url FROM publications "
        "WHERE pdf_url IS NOT NULL AND pdf_url != ''"
    )
    return [(pid, url) for pid, url in cur.fetchall() if not pdf_path_for(pid).exists()]


def run_initial(conn: sqlite3.Connection, limit: int) -> None:
    """Первичный проход: обогащение публикаций абстрактом и (если есть) PDF."""
    api_session = make_api_session()
    browser_session = make_browser_session()

    pubs = fetch_unenriched_publications(conn, limit)
    if not pubs:
        print("Все публикации уже хотя бы раз обогащены — обрабатывать нечего.")
        return

    print(f"Обрабатываю {len(pubs)} публикаций (лимит {limit})")
    stats = {
        "checked": 0,
        "pdf_downloaded": 0,
        "pdf_already": 0,
        "pdf_failed": 0,
        "no_oa": 0,
        "abstract_saved": 0,
        "no_abstract": 0,
    }

    for index, (pub_id, current_pdf_url, current_abstract) in enumerate(pubs, 1):
        print(f"[{index}/{len(pubs)}] {pub_id}")
        stats["checked"] += 1

        data = fetch_paper_data(api_session, pub_id)
        if data is None:
            time.sleep(REQUEST_DELAY)
            continue

        if current_abstract is None:
            if data["abstract"]:
                set_abstract(conn, pub_id, data["abstract"])
                stats["abstract_saved"] += 1
                print(f"  абстракт сохранён ({len(data['abstract'])} симв.)")
            else:
                set_abstract(conn, pub_id, "")
                stats["no_abstract"] += 1
                print("  абстракт в OpenAlex отсутствует")

        if current_pdf_url is None:
            if not data["pdf_url"]:
                set_pdf_url(conn, pub_id, "")
                stats["no_oa"] += 1
                print("  open-access PDF не указан")
            else:
                set_pdf_url(conn, pub_id, data["pdf_url"])
                print(f"  pdf_url: {data['pdf_url']}")
                dest = pdf_path_for(pub_id)
                if dest.exists():
                    stats["pdf_already"] += 1
                    print("  файл уже скачан")
                elif download_pdf(browser_session, data["pdf_url"], dest):
                    size_kb = dest.stat().st_size // 1024
                    stats["pdf_downloaded"] += 1
                    print(f"  PDF скачан ({size_kb} КБ)")
                else:
                    stats["pdf_failed"] += 1

        time.sleep(REQUEST_DELAY)

    print()
    print(f"Проверено публикаций:    {stats['checked']}")
    print(f"PDF скачано:             {stats['pdf_downloaded']}")
    print(f"PDF уже было локально:   {stats['pdf_already']}")
    print(f"Без OA-источника:        {stats['no_oa']}")
    print(f"Ошибок скачивания PDF:   {stats['pdf_failed']}")
    print(f"Абстрактов сохранено:    {stats['abstract_saved']}")
    print(f"Без абстракта:           {stats['no_abstract']}")


def run_retry_failed(conn: sqlite3.Connection) -> None:
    """Повторное скачивание только тех публикаций, у которых на прошлом прогоне
    pdf_url был получен, но сам PDF скачать не удалось (HTTP 403, SSL, captcha)."""
    browser_session = make_browser_session()

    pubs = fetch_failed_pdf_publications(conn)
    if not pubs:
        print("Нет публикаций с упавшим скачиванием — нечего перезапускать.")
        return

    print(f"Перекачиваю {len(pubs)} PDF (через браузерный User-Agent)")
    stats = {"checked": 0, "downloaded": 0, "still_failed": 0}

    for index, (pub_id, pdf_url) in enumerate(pubs, 1):
        print(f"[{index}/{len(pubs)}] {pub_id} | {pdf_url[:80]}")
        stats["checked"] += 1
        dest = pdf_path_for(pub_id)
        if download_pdf(browser_session, pdf_url, dest):
            size_kb = dest.stat().st_size // 1024
            stats["downloaded"] += 1
            print(f"  PDF скачан ({size_kb} КБ)")
        else:
            stats["still_failed"] += 1
        time.sleep(REQUEST_DELAY)

    print()
    print(f"Попыток скачивания:       {stats['checked']}")
    print(f"Стало успешными:          {stats['downloaded']}")
    print(f"Остались упавшими:        {stats['still_failed']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Обогащает публикации абстрактом из OpenAlex и, если доступен open-access "
            "источник, скачивает PDF в data/pdfs/."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=FETCH_BATCH_SIZE,
        help=f"Сколько необработанных публикаций взять за один запуск (по умолчанию: {FETCH_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Не обогащать новые публикации, а перекачать только те, у которых "
            "pdf_url был успешно получен, но файл локально отсутствует. "
            "Использует браузерный User-Agent, что обходит большую часть "
            "anti-bot блокировок."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    try:
        if args.retry_failed:
            run_retry_failed(conn)
        else:
            run_initial(conn, args.limit)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
