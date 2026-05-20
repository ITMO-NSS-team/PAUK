"""Формирует случайную выборку публикаций для ручной оценки качества пайплайна.

Берёт N публикаций случайно из тех, у которых есть хоть какой-то материал -
PDF или абстракт. Гарантирует, что в выборке будет минимум K публикаций с
подтверждённой авторской ссылкой и минимум K с отклонёнными — это нужно,
чтобы разметчик видел обе стороны вердикта LLM. Остальные позиции в выборке
заполняются рандомно из всех публикаций с материалом — там окажутся и те,
у которых вообще не нашлось кандидатов.

Никаких сетевых запросов — всё из БД.

Полная документация метода лежит в ../evaluation/README.md

Запускать из корня проекта:
    uv run python scripts/sample_for_review.py
    uv run python scripts/sample_for_review.py --size 100 --seed 7
"""

import argparse
import json
import random
import sqlite3
from pathlib import Path

from config import DB_PATH, ROOT_DIR

MATERIAL_FILTERS = {
    "pdf": "(pdf_local_path IS NOT NULL AND pdf_local_path != '')",
    "abstract": "(abstract IS NOT NULL AND abstract != '')",
    "both": (
        "(pdf_local_path IS NOT NULL AND pdf_local_path != '')"
        " AND (abstract IS NOT NULL AND abstract != '')"
    ),
    "all": (
        "(pdf_local_path IS NOT NULL AND pdf_local_path != '')"
        " OR (abstract IS NOT NULL AND abstract != '')"
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Случайная выборка для ручной оценки качества пайплайна."
    )
    parser.add_argument(
        "--size",
        type=int,
        default=80,
        help="Размер выборки (по умолчанию 80).",
    )
    parser.add_argument(
        "--material",
        choices=list(MATERIAL_FILTERS.keys()),
        default="all",
        help=(
            "Из каких публикаций брать выборку: "
            "pdf — только у которых скачан PDF; "
            "abstract — только с абстрактом; "
            "both — есть и PDF, и абстракт; "
            "all — у кого есть хоть что-то (по умолчанию)."
        ),
    )
    parser.add_argument(
        "--status",
        choices=["any", "confirmed", "rejected"],
        default="any",
        help=(
            "Какие публикации брать по итоговому вердикту пайплайна: "
            "confirmed — только has_code=1; "
            "rejected — только те, где кандидаты есть, но все отклонены LLM; "
            "any — все (с гарантией минимумов, по умолчанию)."
        ),
    )
    parser.add_argument(
        "--min-confirmed",
        type=int,
        default=5,
        help="Минимум публикаций с has_code=1 в выборке (по умолчанию 5).",
    )
    parser.add_argument(
        "--min-rejected",
        type=int,
        default=5,
        help=(
            "Минимум публикаций, у которых есть кандидаты, но все отклонены LLM "
            "(по умолчанию 5)."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed для воспроизводимости выборки (по умолчанию 42).",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT_DIR / "evaluation" / "sample.jsonl"),
        help="Путь до JSONL-файла.",
    )
    return parser.parse_args()


def fetch_pools(
    conn: sqlite3.Connection, material: str
) -> tuple[list[str], list[str], list[str]]:
    """Делит публикации с материалом на три непересекающиеся группы:

    confirmed — has_code = 1 (есть хотя бы одна подтверждённая ссылка);
    rejected  — есть кандидаты, но все is_relevant = 0;
    plain     — материал есть, кандидатов вообще не нашлось.

    Параметр ``material`` (pdf / abstract / both / all) сужает пул
    публикаций по тому, какой материал у них есть.
    """
    where = MATERIAL_FILTERS[material]
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, has_code,
               (SELECT COUNT(*) FROM repo_links r WHERE r.publication_id = p.id) AS n_candidates
        FROM publications p
        WHERE {where}
        """
    )

    confirmed, rejected, plain = [], [], []
    for pub_id, has_code, n in cur.fetchall():
        if has_code == 1:
            confirmed.append(pub_id)
        elif n > 0:
            rejected.append(pub_id)
        else:
            plain.append(pub_id)
    return confirmed, rejected, plain


def take_sample(
    pools: tuple[list[str], list[str], list[str]],
    status: str,
    total_size: int,
    min_confirmed: int,
    min_rejected: int,
    rng: random.Random,
) -> tuple[list[str], dict[str, int]]:
    """Возвращает список publication_id выборки и breakdown по группам.

    При status='confirmed' или 'rejected' выборка идёт только из
    соответствующей группы, и минимумы игнорируются.
    """
    confirmed, rejected, plain = pools

    if status == "confirmed":
        pool = list(confirmed)
        rng.shuffle(pool)
        selected = pool[:total_size]
    elif status == "rejected":
        pool = list(rejected)
        rng.shuffle(pool)
        selected = pool[:total_size]
    else:  # any
        take_confirmed = min(min_confirmed, len(confirmed))
        take_rejected = min(min_rejected, len(rejected))
        chosen_confirmed = rng.sample(confirmed, take_confirmed)
        chosen_rejected = rng.sample(rejected, take_rejected)
        fixed = set(chosen_confirmed) | set(chosen_rejected)
        remaining_quota = max(0, total_size - len(fixed))
        leftover_pool = [
            pid for pid in (confirmed + rejected + plain) if pid not in fixed
        ]
        rng.shuffle(leftover_pool)
        chosen_random = leftover_pool[:remaining_quota]
        selected = list(fixed) + chosen_random

    confirmed_set = set(confirmed)
    rejected_set = set(rejected)
    breakdown = {
        "confirmed": sum(1 for p in selected if p in confirmed_set),
        "rejected": sum(1 for p in selected if p in rejected_set),
        "plain": sum(
            1 for p in selected if p not in confirmed_set and p not in rejected_set
        ),
    }
    return selected, breakdown


def make_record(conn: sqlite3.Connection, pub_id: str) -> dict:
    """Собирает одну запись JSONL по publication_id (метаданные + кандидаты)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, doi, openalex_url, authors, abstract,
               pdf_url, pdf_local_path, has_code, code_url
        FROM publications WHERE id = ?
        """,
        (pub_id,),
    )
    row = cur.fetchone()

    abstract = row[5] or ""
    code_url_raw = row[9]
    try:
        code_url_parsed = json.loads(code_url_raw) if code_url_raw else None
    except (TypeError, json.JSONDecodeError):
        code_url_parsed = code_url_raw

    record = {
        "publication_id": row[0],
        "title": row[1],
        "doi": row[2],
        "openalex_url": row[3],
        "authors": row[4],
        "abstract_preview": abstract[:500],
        "pdf_url": row[6],
        "pdf_local_path": row[7],
        "current_has_code": row[8],
        "current_code_url": code_url_parsed,
        "candidates": [],
        "manual_review": {
            "really_has_code": None,  # true / false / null
            "actual_repo_urls": [],
            "comment": "",
        },
    }

    cur.execute(
        """
        SELECT url, host, page_number, context,
               is_relevant, llm_confidence, llm_reason
        FROM repo_links WHERE publication_id = ?
        ORDER BY id
        """,
        (pub_id,),
    )
    for url, host, page, context, is_relevant, conf, reason in cur.fetchall():
        if is_relevant == 1:
            verdict = "ДА"
        elif is_relevant == 0:
            verdict = "нет"
        else:
            verdict = "не классифицировано"
        record["candidates"].append(
            {
                "url": url,
                "host": host,
                "page_number": page,
                "context": context,
                "llm_verdict": verdict,
                "is_relevant": is_relevant,
                "llm_confidence": conf,
                "llm_reason": reason,
                "manual_correct": None,
            }
        )
    return record


def main() -> None:
    args = parse_args()
    if args.status == "any" and args.size < args.min_confirmed + args.min_rejected:
        raise SystemExit(
            f"--size ({args.size}) меньше суммы минимумов "
            f"({args.min_confirmed} + {args.min_rejected})"
        )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    conn = sqlite3.connect(DB_PATH)
    try:
        pools = fetch_pools(conn, args.material)
        confirmed, rejected, plain = pools
        total = len(confirmed) + len(rejected) + len(plain)
        print(f"Фильтры: --material {args.material}  --status {args.status}")
        print(f"В БД подходит публикаций по material: {total}")
        print(f"  с has_code=1 (подтверждённые):       {len(confirmed)}")
        print(f"  с кандидатами, но все отклонены LLM: {len(rejected)}")
        print(f"  без кандидатов:                       {len(plain)}")
        print()

        if total == 0:
            print("Пул пустой — нечего выгружать. Попробуй другой --material.")
            return
        if args.status == "confirmed" and not confirmed:
            print("В выбранном --material нет подтверждённых публикаций.")
            return
        if args.status == "rejected" and not rejected:
            print("В выбранном --material нет отклонённых публикаций.")
            return

        selected, breakdown = take_sample(
            pools,
            status=args.status,
            total_size=args.size,
            min_confirmed=args.min_confirmed,
            min_rejected=args.min_rejected,
            rng=rng,
        )

        with out_path.open("w", encoding="utf-8") as f:
            for pub_id in selected:
                rec = make_record(conn, pub_id)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        print(f"Сохранено {len(selected)} публикаций в {out_path}")
        print(f"  подтверждённых: {breakdown['confirmed']}")
        print(f"  отклонённых:    {breakdown['rejected']}")
        print(f"  без кандидатов: {breakdown['plain']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
