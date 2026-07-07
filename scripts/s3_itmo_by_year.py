"""Study 2: сколько ITMO-публикаций есть в S3-зеркале, по годам.

Тянет ITMO-работы из OpenAlex за диапазон, покрытый зеркалом (по умолчанию
2007–2021) — только метаданные works (БЕЗ запросов /authors, поэтому быстро),
и для каждой проверяет наличие в S3 (bioRxiv/medRxiv по DOI, arXiv по id).
Выводит по годам, сколько ITMO-публикаций найдено в зеркале, и сохраняет
график + CSV. Основную БД пайплайна НЕ трогает.

Запуск из корня проекта:
    uv run python scripts/s3_itmo_by_year.py
    uv run python scripts/s3_itmo_by_year.py --start-date 2007-01-01 --end-date 2022-01-01
"""

import argparse
import csv
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import plotly.graph_objects as go
import requests

from config import (
    ITMO_ROR_ID,
    OPENALEX_API_KEY,
    OPENALEX_WORKS_URL,
    ROOT_DIR,
    S3_ENDPOINT_URL,
    USER_AGENT,
)
from fetch_pdfs_s3 import make_s3_client, normalize_doi, resolve

WORKERS = 12
SELECT = "id,doi,publication_year,primary_location,best_oa_location"


def openalex_params(extra: dict) -> dict:
    """Параметры запроса плюс API-ключ, если он задан в .env."""
    params = dict(extra)
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    return params


def iter_itmo_works(session: requests.Session, start_date: str, end_date: str) -> list[dict]:
    """Постранично выгружает ITMO-работы за период (works-only, cursor-пагинация)."""
    all_works: list[dict] = []
    cursor = "*"
    filter_q = (
        f"authorships.institutions.ror:{ITMO_ROR_ID},"
        f"publication_date:>{start_date},publication_date:<{end_date}"
    )
    while True:
        params = openalex_params(
            {"filter": filter_q, "select": SELECT, "per-page": 200, "cursor": cursor}
        )
        resp = session.get(OPENALEX_WORKS_URL, params=params, timeout=60)
        if resp.status_code == 429:
            print("  429 от OpenAlex, sleep 60 сек и повтор")
            time.sleep(60)
            continue
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        all_works.extend(results)
        print(f"  выгружено {len(all_works)}")
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
    return all_works


def candidate_url(work: dict) -> str | None:
    """arXiv-ссылка среди locations (для arXiv-работ без DataCite-DOI)."""
    for loc in (work.get("best_oa_location"), work.get("primary_location")):
        if not loc:
            continue
        for key in ("pdf_url", "landing_page_url"):
            url = loc.get(key)
            if url and "arxiv.org" in url.lower():
                return url
    return None


def match_one(s3, work: dict) -> tuple[object, bool]:
    """(год, есть_ли_в_S3) для одной работы; ошибки резолва трактуются как «нет»."""
    year = work.get("publication_year")
    try:
        hit = resolve(s3, normalize_doi(work.get("doi")), candidate_url(work))
    except Exception:  # noqa: BLE001 — единичный сбой S3 не должен ронять весь прогон
        hit = None
    return (year if year is not None else "—"), bool(hit)


def save_outputs(rows: list[tuple], out_dir: Path) -> None:
    """CSV + bar-график «ITMO-публикаций в S3 по годам» (HTML, и PNG если можно)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "s3_itmo_by_year.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["year", "itmo_total", "in_s3"])
        writer.writerows(rows)

    years = [str(r[0]) for r in rows]
    in_s3 = [r[2] for r in rows]
    fig = go.Figure(go.Bar(x=years, y=in_s3, marker_color="#3182bd"))
    fig.update_layout(
        title="ITMO-публикаций в S3-зеркале по годам",
        template="plotly_white",
    )
    fig.update_xaxes(title_text="Год", dtick=1)
    fig.update_yaxes(title_text="Публикаций в S3", rangemode="tozero")
    fig.write_html(out_dir / "s3_itmo_by_year.html")
    try:
        fig.write_image(out_dir / "s3_itmo_by_year.png", width=1000, height=560, scale=2)
    except Exception as exc:  # noqa: BLE001 — kaleido/браузер могут отсутствовать
        print(f"PNG не сохранён ({type(exc).__name__}); HTML готов.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Считает ITMO-публикации, присутствующие в S3-зеркале, по годам."
    )
    parser.add_argument("--start-date", default="2007-01-01", help="нижняя граница периода.")
    parser.add_argument("--end-date", default="2022-01-01", help="верхняя граница (исключая).")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT_DIR / "reports" / "study2_s3_by_year",
        help="Куда сохранять CSV/график.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not S3_ENDPOINT_URL:
        print("S3_ENDPOINT_URL не задан в .env — нечего проверять, выхожу.")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    print(f"Тяну ITMO-работы {args.start_date}..{args.end_date} из OpenAlex (без авторов)...")
    works = iter_itmo_works(session, args.start_date, args.end_date)
    print(f"Всего работ: {len(works)}. Матчу против S3 в {WORKERS} потоков...")

    s3 = make_s3_client()
    total_by_year: dict[object, int] = defaultdict(int)
    s3_by_year: dict[object, int] = defaultdict(int)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for year, in_s3 in pool.map(lambda w: match_one(s3, w), works):
            total_by_year[year] += 1
            if in_s3:
                s3_by_year[year] += 1

    rows = [
        (year, total_by_year[year], s3_by_year.get(year, 0))
        for year in sorted(total_by_year, key=lambda k: (isinstance(k, str), k))
    ]

    print(f"\n{'Год':>6} {'ITMO всего':>11} {'В S3':>6}")
    for year, total, found in rows:
        print(f"{str(year):>6} {total:>11} {found:>6}")
    print(f"\nИтого ITMO в S3: {sum(r[2] for r in rows)} из {sum(r[1] for r in rows)}")

    save_outputs(rows, args.out_dir)
    print(f"Результаты: {args.out_dir}")


if __name__ == "__main__":
    main()
