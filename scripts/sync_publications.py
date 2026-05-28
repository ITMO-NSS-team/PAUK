"""Прокидывает подтверждённые ссылки из repo_links в publications.

После того как classify_repo_links.py заполнил is_relevant у каждой
кандидатной ссылки, нам нужно отметить публикации, у которых есть хоть
один подтверждённый "авторский" репозиторий:

  - publications.has_code = 1
  - publications.code_url  = JSON-массив всех подтверждённых URL,
    отсортированный от максимальной llm_confidence к минимальной.

Запускать из корня проекта:
    uv run python scripts/sync_publications.py
"""

import json
import sqlite3

from config import DB_PATH


def sync(conn: sqlite3.Connection) -> dict[str, int]:
    """Для каждой публикации с подтверждёнными ссылками обновляет publications."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT publication_id, url
        FROM repo_links
        WHERE is_relevant = 1
        ORDER BY
            publication_id,
            COALESCE(llm_confidence, 0) DESC,
            id ASC
        """
    )
    rows = cur.fetchall()

    urls_per_pub: dict[str, list[str]] = {}
    for pub_id, url in rows:
        urls_per_pub.setdefault(pub_id, []).append(url)

    updated = 0
    with_multiple = 0
    for pub_id, urls in urls_per_pub.items():
        cur.execute(
            "UPDATE publications SET has_code = 1, code_url = ? WHERE id = ?",
            (json.dumps(urls, ensure_ascii=False), pub_id),
        )
        if cur.rowcount > 0:
            updated += 1
        if len(urls) > 1:
            with_multiple += 1

    conn.commit()
    return {"updated": updated, "with_multiple": with_multiple}


def print_summary(conn: sqlite3.Connection) -> None:
    """Печатает текущее состояние publications.has_code по всей БД."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM publications")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM publications WHERE has_code = 1")
    with_code = cur.fetchone()[0]
    cur.execute(
        """
        SELECT COUNT(DISTINCT publication_id)
        FROM repo_links
        WHERE is_relevant = 0
        """
    )
    only_rejected = cur.fetchone()[0]

    print()
    print(f"Всего публикаций в БД:                  {total}")
    print(f"С подтверждённым репозиторием (has_code=1): {with_code}")
    print(f"  (это {with_code / total * 100:.1f}% от базы)")
    print(f"С кандидатами, но все отклонены LLM:    {only_rejected}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        stats = sync(conn)
        print(f"Публикаций обновлено: {stats['updated']}")
        if stats["with_multiple"]:
            print(
                f"  из них с несколькими ссылками в JSON-массиве: {stats['with_multiple']}"
            )
        print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
