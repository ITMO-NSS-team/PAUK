"""Чистит существующие URL в repo_links под актуальные правила экстракции.

Прогоняет ту же нормализацию, что использует extract_repo_links.py
(`clean_url_tail`), по уже сохранённым строкам repo_links. Если URL после
очистки совпал с уже существующей строкой той же публикации — лишняя
строка удаляется (приоритет у строки с is_relevant = 1).

После очистки можно сразу запустить sync_publications.py, чтобы
publications.code_url пересобрался без «грязных» значений. Никаких
сетевых запросов не делает.

Запускать из корня проекта:
    uv run python scripts/cleanup_repo_links.py
"""

import sqlite3

from config import DB_PATH
from extract_repo_links import clean_url_tail


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        "SELECT id, publication_id, url, is_relevant FROM repo_links ORDER BY id"
    )
    rows = cur.fetchall()
    if not rows:
        print("В repo_links пусто, чистить нечего.")
        return

    # сначала вычислим новый URL для каждой строки
    cleaned: list[tuple[int, str, str, str, int | None]] = []
    changed_count = 0
    for row_id, pub_id, url, is_relevant in rows:
        new_url = clean_url_tail(url)
        if new_url != url:
            changed_count += 1
        cleaned.append((row_id, pub_id, url, new_url, is_relevant))

    # сгруппируем по (pub_id, new_url), чтобы найти дубли после очистки
    groups: dict[tuple[str, str], list[tuple[int, str, int | None]]] = {}
    for row_id, pub_id, old_url, new_url, is_relevant in cleaned:
        groups.setdefault((pub_id, new_url), []).append((row_id, old_url, is_relevant))

    updated = 0
    deleted = 0
    for (pub_id, new_url), members in groups.items():
        if len(members) == 1:
            row_id, old_url, _ = members[0]
            if new_url != old_url:
                cur.execute(
                    "UPDATE repo_links SET url = ? WHERE id = ?", (new_url, row_id)
                )
                updated += 1
            continue

        # есть дубли: оставляем одну строку, приоритет is_relevant=1
        members_sorted = sorted(
            members, key=lambda m: (0 if m[2] == 1 else 1, m[0])
        )
        keeper_id, keeper_old_url, _ = members_sorted[0]
        if new_url != keeper_old_url:
            cur.execute(
                "UPDATE repo_links SET url = ? WHERE id = ?", (new_url, keeper_id)
            )
            updated += 1
        for dup_id, _, _ in members_sorted[1:]:
            cur.execute("DELETE FROM repo_links WHERE id = ?", (dup_id,))
            deleted += 1

    conn.commit()
    conn.close()

    print(f"Просмотрено строк:                {len(rows)}")
    print(f"URL изменены пост-обработкой:     {changed_count}")
    print(f"Обновлено строк в БД:             {updated}")
    print(f"Удалено как дубли:                {deleted}")


if __name__ == "__main__":
    main()
