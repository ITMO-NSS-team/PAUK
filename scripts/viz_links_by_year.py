"""Строит визуализацию числа найденных код-ссылок по годам публикаций.

Считает из БД по годам:
  * total       — всего публикаций;
  * with_code    — публикаций с авторским репо (publications.has_code = 1);
  * links        — подтверждённых LLM ссылок (repo_links.is_relevant = 1);
  * coverage %   — доля публикаций с кодом (with_code / total).

Рисует сгруппированные столбцы (with_code и links) и линию покрытия на
второй оси, сохраняет интерактивный HTML и статичный PNG.

Запускать из корня проекта (после прогона пайплайна):
    uv run python scripts/viz_links_by_year.py
    uv run python scripts/viz_links_by_year.py --out-dir reports --min-year 2021

PNG-экспорт требует kaleido + браузер; если он недоступен, HTML всё равно
сохраняется, а про PNG выводится предупреждение.
"""

import argparse
import sqlite3
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import DB_PATH, ROOT_DIR


def fetch_year_stats(
    conn: sqlite3.Connection, min_year: int | None, max_year: int | None
) -> list[dict]:
    """Возвращает по годам: total, with_code, links (отсортировано по году).

    Год берётся из publications.year, при NULL — из первых 4 символов
    publication_date. Публикации без распознаваемого года отбрасываются.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        WITH pub AS (
            SELECT
                id,
                has_code,
                COALESCE(year, CAST(substr(publication_date, 1, 4) AS INTEGER)) AS yr
            FROM publications
        )
        SELECT
            pub.yr                                        AS year,
            COUNT(*)                                      AS total,
            SUM(CASE WHEN pub.has_code = 1 THEN 1 ELSE 0 END) AS with_code,
            (
                SELECT COUNT(*)
                FROM repo_links rl
                JOIN pub p2 ON p2.id = rl.publication_id
                WHERE rl.is_relevant = 1 AND p2.yr = pub.yr
            )                                             AS links
        FROM pub
        WHERE pub.yr IS NOT NULL
        GROUP BY pub.yr
        ORDER BY pub.yr
        """
    ).fetchall()

    stats = [dict(r) for r in rows]
    if min_year is not None:
        stats = [s for s in stats if s["year"] >= min_year]
    if max_year is not None:
        stats = [s for s in stats if s["year"] <= max_year]
    return stats


def build_figure(stats: list[dict]) -> go.Figure:
    """Сгруппированные столбцы with_code/links + линия покрытия на 2-й оси."""
    years = [s["year"] for s in stats]
    with_code = [s["with_code"] or 0 for s in stats]
    links = [s["links"] or 0 for s in stats]
    coverage = [
        round(100 * (s["with_code"] or 0) / s["total"], 1) if s["total"] else 0.0
        for s in stats
    ]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(name="Публикаций с кодом", x=years, y=with_code, marker_color="#3182bd"),
        secondary_y=False,
    )
    fig.add_trace(
        go.Bar(name="Подтверждённых ссылок", x=years, y=links, marker_color="#e6550d"),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            name="Покрытие, %",
            x=years,
            y=coverage,
            mode="lines+markers",
            marker_color="#31a354",
        ),
        secondary_y=True,
    )
    fig.update_layout(
        title="Код-ссылки в публикациях ИТМО по годам",
        barmode="group",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_xaxes(title_text="Год", dtick=1)
    fig.update_yaxes(title_text="Количество", secondary_y=False)
    fig.update_yaxes(title_text="Покрытие, %", secondary_y=True, rangemode="tozero")
    return fig


def save_outputs(fig: go.Figure, out_dir: Path) -> None:
    """Сохраняет HTML (всегда) и PNG (если доступен рендер kaleido/браузер)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "links_by_year.html"
    fig.write_html(html_path)
    print(f"HTML сохранён: {html_path}")

    png_path = out_dir / "links_by_year.png"
    try:
        fig.write_image(png_path, width=1000, height=560, scale=2)
        print(f"PNG сохранён:  {png_path}")
    except Exception as exc:  # noqa: BLE001 — kaleido/браузер могут отсутствовать
        print(
            f"PNG не сохранён ({type(exc).__name__}: {exc}).\n"
            "  Для PNG нужен браузер для kaleido — установите командой "
            "`uv run plotly_get_chrome` и повторите. HTML уже готов."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Строит график числа найденных код-ссылок по годам публикаций."
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT_DIR / "reports",
        help="Куда сохранять графики (по умолчанию: reports/).",
    )
    parser.add_argument("--min-year", type=int, default=None, help="Нижняя граница года.")
    parser.add_argument("--max-year", type=int, default=None, help="Верхняя граница года.")
    parser.add_argument(
        "--db", type=Path, default=DB_PATH, help="Путь к SQLite-БД (по умолчанию из config)."
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = sqlite3.connect(args.db)
    try:
        stats = fetch_year_stats(conn, args.min_year, args.max_year)
    finally:
        conn.close()

    if not stats:
        print("Нет данных с распознаваемым годом — график не построен.")
        return

    print(f"{'Год':>6} {'Всего':>8} {'С кодом':>8} {'Ссылок':>8} {'Покрытие':>9}")
    for s in stats:
        cov = 100 * (s["with_code"] or 0) / s["total"] if s["total"] else 0
        print(
            f"{s['year']:>6} {s['total']:>8} {s['with_code'] or 0:>8} "
            f"{s['links'] or 0:>8} {cov:>8.1f}%"
        )

    fig = build_figure(stats)
    save_outputs(fig, args.out_dir)


if __name__ == "__main__":
    main()
