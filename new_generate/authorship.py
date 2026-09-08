"""Индекс авторства: кто с кем, какие публикации вообще попадают в граф.

Один вызов на весь прогон, без повторного использования с разной
конфигурацией — простая функция, а не класс, ей нечего держать как
состояние между вызовами.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Authorship:
    """Кто с кем: индекс авторства и публикации, отфильтрованные до тех,
    у кого есть хотя бы один ИТМО-автор."""

    pub_authors: dict[str, list[str]]
    """Публикация -> список её ИТМО-авторов."""
    author_pubs: dict[str, list[str]]
    """Автор -> список его публикаций (тех, что попали в pub_authors)."""
    pubs_rows: list[dict]
    """Строки db["publications"], отфильтрованные до pub_ids."""
    pub_ids: set[str]
    """id публикаций с хотя бы одним ИТМО-автором."""


def build_authorship_index(db: dict[str, list[dict]]) -> Authorship:
    """Строит индекс авторства и отсеивает публикации без ИТМО-авторов.

    Аргументы:
        db: Снепшот графа в форме `new_cache::load_db()`.

    Возвращает:
        `Authorship` с индексами в обе стороны и отфильтрованным списком публикаций.
    """
    pub_authors: dict[str, list[str]] = defaultdict(list)
    author_pubs: dict[str, list[str]] = defaultdict(list)
    for row in db["authorship"]:
        pid, per = row["pid"], row["per"]
        pub_authors[pid].append(per)
        author_pubs[per].append(pid)

    pubs_rows = [r for r in db["publications"] if r["id"] in pub_authors]
    pub_ids = {r["id"] for r in pubs_rows}
    logger.info("Публикаций с ИТМО-авторами: %d из %d", len(pubs_rows), len(db["publications"]))
    return Authorship(dict(pub_authors), dict(author_pubs), pubs_rows, pub_ids)
