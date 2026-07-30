import argparse
import json
import logging
import sqlite3
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

import requests
from data_enrichment.config import (
    AUTHORS_CACHE_CAPACITY,
    DB_PATH,
    ITMO_NAMES,
    ITMO_ROR_ID,
    OPENALEX_API_KEY,
    OPENALEX_AUTHORS_URL,
    OPENALEX_WORKS_URL,
    REQUEST_DELAY,
    USER_AGENT,
)

AFFILIATION_SEPARATOR = " \n "


class LRUCache:
    """Простой LRU-кэш с ограничением по ёмкости, ключ - OpenAlex author ID."""

    def __init__(self, capacity: int = AUTHORS_CACHE_CAPACITY) -> None:
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._capacity = capacity

    def get(self, key: str) -> Any | None:
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def put(self, key: str, value: Any) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)


class PublicationsIngestor:
    """Управляет загрузкой публикаций из OpenAlex в SQLite за заданный период."""

    def __init__(self, db_path, start_date: str, end_date: str) -> None:
        self.db_path = db_path
        self.start_date = start_date
        self.end_date = end_date

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        self.authors_cache = LRUCache()
        self.conn: sqlite3.Connection | None = None
        self.cursor: sqlite3.Cursor | None = None

        self.stats = {
            "papers_processed": 0,
            "papers_with_funding": 0,
            "new_itmo_persons": 0,
            "new_external_persons": 0,
            "authorships_created": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "authors_skipped": 0,
        }
        self.api_stats = {
            "works_requests": 0,
            "authors_requests": 0,
            "total_requests": 0,
        }

    # --- Управление соединением -----------------------------------------

    def connect(self) -> None:
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.cursor = self.conn.cursor()

    def disconnect(self) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None
            self.cursor = None

    # --- Запросы к OpenAlex ---------------------------------------------

    def _api_params(self, extra: dict | None = None) -> dict:
        """Базовые параметры запроса плюс API-ключ, если он задан в .env."""
        params = dict(extra or {})
        if OPENALEX_API_KEY:
            params["api_key"] = OPENALEX_API_KEY
        return params

    def fetch_papers_by_date_range(self, per_page: int = 200) -> list[dict]:
        """Постранично выгружает все работы ИТМО за период через cursor-пагинацию."""
        all_papers: list[dict] = []
        cursor = "*"
        page = 1
        filter_query = (
            f"authorships.institutions.ror:{ITMO_ROR_ID},"
            f"publication_date:>{self.start_date},"
            f"publication_date:<{self.end_date}"
        )

        logger.info("Запрашиваю работы ИТМО из OpenAlex (ROR %s)", ITMO_ROR_ID)
        logger.info("Период: %s -> %s", self.start_date, self.end_date)

        while True:
            params = self._api_params(
                {
                    "filter": filter_query,
                    "sort": "publication_date:desc",
                    "per-page": per_page,
                    "cursor": cursor,
                }
            )
            try:
                response = self.session.get(OPENALEX_WORKS_URL, params=params)
                self.api_stats["works_requests"] += 1
                self.api_stats["total_requests"] += 1
                if response.status_code == 429:
                    logger.warning("страница %d: 429, sleep 60 сек и повтор", page)
                    time.sleep(60)
                    continue
                response.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("запрос упал: %s", exc)
                break

            data = response.json()
            results = data.get("results", [])
            if not results:
                break

            all_papers.extend(results)
            logger.info("страница %d: +%d (всего %d)", page, len(results), len(all_papers))

            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor:
                break
            page += 1
            time.sleep(REQUEST_DELAY)

        logger.info("Получено работ: %d", len(all_papers))
        return all_papers

    def fetch_openalex_author(self, author_id: str, retries: int = 3) -> dict | None:
        """Возвращает полную карточку автора из /authors, используя LRU-кэш.

        При 429 ждёт минуту и повторяет, но не более ``retries`` раз - иначе
        получили бы бесконечный цикл на устойчивом rate-limit.
        """
        if not author_id or author_id == "None":
            return None
        cached = self.authors_cache.get(author_id)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached
        self.stats["cache_misses"] += 1

        url = f"{OPENALEX_AUTHORS_URL}/{author_id}"
        try:
            response = self.session.get(url, params=self._api_params())
            self.api_stats["authors_requests"] += 1
            self.api_stats["total_requests"] += 1
        except requests.RequestException as exc:
            logger.warning("запрос /authors упал для %s: %s", author_id, exc)
            return None

        if response.status_code == 200:
            data = response.json()
            self.authors_cache.put(author_id, data)
            return data
        if response.status_code == 429 and retries > 0:
            logger.warning("OpenAlex 429 для %s, sleep 60 сек (осталось попыток: %d)", author_id, retries - 1)
            time.sleep(60)
            return self.fetch_openalex_author(author_id, retries - 1)
        return None

    # --- Вспомогательные методы -----------------------------------------

    @staticmethod
    def extract_funding_info(work: dict) -> str | None:
        """Сериализует список грантов OpenAlex в компактный JSON или возвращает None."""
        grants = work.get("grants") or []
        if not grants:
            return None
        funding = [
            {"funder": g.get("funder_display_name"), "grant_id": g.get("grant_id")}
            for g in grants
        ]
        return json.dumps(funding, ensure_ascii=False)

    @staticmethod
    def is_itmo_affiliation(raw_affils: list[str], institutions: list[dict]) -> bool:
        """True, если хоть одна институция или строка аффилиации указывает на ИТМО."""
        for inst in institutions:
            if ITMO_ROR_ID in inst.get("id", ""):
                return True
        for inst in institutions:
            name = inst.get("display_name", "")
            if any(itmo.lower() in name.lower() for itmo in ITMO_NAMES):
                return True
        for affiliation in raw_affils:
            if any(itmo.lower() in affiliation.lower() for itmo in ITMO_NAMES):
                return True
        return False

    def _update_person_affiliation(
        self, table: str, person_id: str, new_affiliations: list[str]
    ) -> None:
        """Добавляет новые строки аффилиации к уже сохранённому набору у человека."""
        self.cursor.execute(
            f"SELECT affiliation FROM {table} WHERE id = ?", (person_id,)
        )
        row = self.cursor.fetchone()
        current = set(row[0].split(AFFILIATION_SEPARATOR)) if row and row[0] else set()
        changed = False
        for aff in new_affiliations:
            aff = aff.strip()
            if aff and aff not in current:
                current.add(aff)
                changed = True
        if changed:
            value = AFFILIATION_SEPARATOR.join(sorted(current))
            self.cursor.execute(
                f"UPDATE {table} SET affiliation = ? WHERE id = ?", (value, person_id)
            )

    def _find_existing_person(
        self, table: str, name_en: str, variants: list[str]
    ) -> str | None:
        """Ищет уже сохранённого человека по name_en или по совпадению вариантов имени."""
        self.cursor.execute(f"SELECT id FROM {table} WHERE name_en = ?", (name_en,))
        row = self.cursor.fetchone()
        if row:
            return row[0]

        self.cursor.execute(
            f"SELECT id, name_variants FROM {table} WHERE name_variants IS NOT NULL"
        )
        for pid, variants_json in self.cursor.fetchall():
            if not variants_json:
                continue
            try:
                existing = json.loads(variants_json)
            except (TypeError, ValueError):
                continue
            if name_en in existing or any(v in existing for v in variants):
                return pid
        return None

    def _merge_variants(self, table: str, person_id: str, variants: list[str]) -> None:
        """Доливает новые варианты имени в name_variants, если их там ещё нет."""
        self.cursor.execute(
            f"SELECT name_variants FROM {table} WHERE id = ?", (person_id,)
        )
        row = self.cursor.fetchone()
        if not (row and row[0]):
            return
        try:
            current = json.loads(row[0])
        except (TypeError, ValueError):
            return
        added = False
        for variant in variants:
            if variant not in current:
                current.append(variant)
                added = True
        if added:
            self.cursor.execute(
                f"UPDATE {table} SET name_variants = ? WHERE id = ?",
                (json.dumps(current, ensure_ascii=False), person_id),
            )

    # --- Создание персон -----------------------------------------------

    def get_or_create_itmo_person(
        self, openalex_id: str, author_data: dict, raw_affiliations: list[str]
    ) -> str | None:
        return self._get_or_create_person(
            table="persons_itmo",
            id_prefix="itmo_",
            openalex_id=openalex_id,
            author_data=author_data,
            raw_affiliations=raw_affiliations,
            counter_key="new_itmo_persons",
        )

    def get_or_create_external_person(
        self, openalex_id: str, author_data: dict, raw_affiliations: list[str]
    ) -> str | None:
        return self._get_or_create_person(
            table="persons_external",
            id_prefix="ext_",
            openalex_id=openalex_id,
            author_data=author_data,
            raw_affiliations=raw_affiliations,
            counter_key="new_external_persons",
        )

    def _get_or_create_person(
        self,
        table: str,
        id_prefix: str,
        openalex_id: str,
        author_data: dict,
        raw_affiliations: list[str],
        counter_key: str,
    ) -> str | None:
        """Находит или создаёт строку в persons_itmo / persons_external."""
        if not openalex_id:
            return None
        display_name = author_data.get("display_name", "Unknown")
        variants = list(author_data.get("display_name_alternatives") or [])
        if display_name not in variants:
            variants.append(display_name)

        existing_id = self._find_existing_person(table, display_name, variants)
        if existing_id:
            self._merge_variants(table, existing_id, variants)
            if raw_affiliations:
                self._update_person_affiliation(table, existing_id, raw_affiliations)
            return existing_id

        person_id = f"{id_prefix}{openalex_id}"
        self.cursor.execute(f"SELECT 1 FROM {table} WHERE id = ?", (person_id,))
        if self.cursor.fetchone():
            return person_id

        affiliation_text = (
            AFFILIATION_SEPARATOR.join(sorted(set(raw_affiliations)))
            if raw_affiliations
            else None
        )
        email = (author_data.get("ids") or {}).get("email")

        try:
            self.cursor.execute(
                f"""
                INSERT INTO {table} (id, name_en, name_variants, affiliation, email)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    display_name,
                    json.dumps(variants, ensure_ascii=False),
                    affiliation_text,
                    email,
                ),
            )
            self.stats[counter_key] += 1
            return person_id
        except sqlite3.IntegrityError as exc:
            logger.warning("ошибка вставки в %s %s: %s", table, person_id, exc)
            return None

    # --- Сохранение публикации -----------------------------------------

    def add_publication(self, work: dict, index: int, total: int) -> None:
        """Сохраняет одну работу OpenAlex и связанные с ней строки авторства."""
        work_id = work.get("id", "").replace("https://openalex.org/", "")
        if not work_id:
            logger.warning("[%d/%d] пропуск: нет id", index, total)
            return

        self.cursor.execute("SELECT 1 FROM publications WHERE id = ?", (work_id,))
        if self.cursor.fetchone():
            logger.info("[%d/%d] %s уже есть в БД, пропуск", index, total, work_id)
            return

        title = work.get("title") or "No title"
        title_preview = title[:60] + "..." if len(title) > 60 else title
        logger.info("[%d/%d] %s | %s", index, total, work_id, title_preview)

        funding = self.extract_funding_info(work)
        if funding:
            self.stats["papers_with_funding"] += 1

        doi = work.get("doi")
        pub_date = work.get("publication_date")
        year = int(pub_date[:4]) if pub_date else None
        primary_source = (work.get("primary_location") or {}).get("source") or {}
        journal = primary_source.get("display_name")

        try:
            self.cursor.execute(
                """
                INSERT INTO publications
                    (id, title, journal, doi, publication_date, year, funding, openalex_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (work_id, title, journal, doi, pub_date, year, funding, work.get("id")),
            )
        except sqlite3.IntegrityError as exc:
            logger.warning("ошибка вставки публикации %s: %s", work_id, exc)
            return

        authorships = work.get("authorships") or []
        author_names: list[str] = []
        all_affiliations: set[str] = set()

        for position, authorship in enumerate(authorships, 1):
            author_data = authorship.get("author") or {}
            author_name = author_data.get("display_name", "Unknown")
            author_names.append(author_name)

            raw_affils = authorship.get("raw_affiliation_strings") or []
            all_affiliations.update(raw_affils)

            raw_id = author_data.get("id")
            if not raw_id:
                self.stats["authors_skipped"] += 1
                continue
            openalex_author_id = raw_id.replace("https://openalex.org/", "")
            if not openalex_author_id:
                self.stats["authors_skipped"] += 1
                continue

            full_author = self.fetch_openalex_author(openalex_author_id) or {
                "id": raw_id,
                "display_name": author_name,
                "display_name_alternatives": [],
            }

            is_itmo = self.is_itmo_affiliation(
                raw_affils, authorship.get("institutions") or []
            )
            if is_itmo:
                person_id = self.get_or_create_itmo_person(
                    openalex_author_id, full_author, raw_affils
                )
                person_type = "itmo"
            else:
                person_id = self.get_or_create_external_person(
                    openalex_author_id, full_author, raw_affils
                )
                person_type = "external"

            if not person_id:
                continue

            try:
                self.cursor.execute(
                    """
                    INSERT OR IGNORE INTO publication_authors
                        (publication_id, person_id, person_type, author_position,
                         affiliation_string, is_corresponding)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        person_id,
                        person_type,
                        position,
                        "; ".join(raw_affils) if raw_affils else None,
                        1 if authorship.get("is_corresponding") else 0,
                    ),
                )
                if self.cursor.rowcount > 0:
                    self.stats["authorships_created"] += 1
            except sqlite3.Error as exc:
                logger.warning("ошибка вставки авторства: %s", exc)

        self.cursor.execute(
            "UPDATE publications SET authors = ?, affiliation = ? WHERE id = ?",
            (
                "; ".join(author_names),
                AFFILIATION_SEPARATOR.join(sorted(all_affiliations))
                if all_affiliations
                else None,
                work_id,
            ),
        )
        self.stats["papers_processed"] += 1

        if self.stats["papers_processed"] % 5 == 0:
            self.conn.commit()

    # --- Отчёт ----------------------------------------------------------

    def print_summary(self) -> None:
        cache_total = self.stats["cache_hits"] + self.stats["cache_misses"]
        cache_info = (
            f", кэш: {self.stats['cache_hits']}/{cache_total} ({self.stats['cache_hits'] / cache_total * 100:.0f}%)"
            if cache_total else ""
        )
        logger.info(
            "Итог: публикаций %d (грантов %d), ИТМО +%d, внешних +%d, авторств %d, пропущено %d"
            " | /works %d, /authors %d, всего %d%s",
            self.stats["papers_processed"], self.stats["papers_with_funding"],
            self.stats["new_itmo_persons"], self.stats["new_external_persons"],
            self.stats["authorships_created"], self.stats["authors_skipped"],
            self.api_stats["works_requests"], self.api_stats["authors_requests"],
            self.api_stats["total_requests"], cache_info,
        )

    # --- Драйвер --------------------------------------------------------

    def run(self) -> None:
        self.connect()
        try:
            papers = self.fetch_papers_by_date_range()
            if not papers:
                logger.info("Загружать нечего.")
                return
            logger.info("Сохраняю %d публикаций", len(papers))
            for index, paper in enumerate(papers, 1):
                self.add_publication(paper, index, len(papers))
                time.sleep(0.05)
            self.conn.commit()
            self.print_summary()
        except KeyboardInterrupt:
            logger.warning("Прервано пользователем, коммичу что успели сохранить.")
            if self.conn is not None:
                self.conn.commit()
            self.print_summary()
        except Exception:
            logger.exception("populate_publications упал с ошибкой")
            raise
        finally:
            self.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Загружает публикации сотрудников ИТМО из OpenAlex в локальную БД."
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="Нижняя граница периода (включительно), формат YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="Верхняя граница периода (не включительно), формат YYYY-MM-DD.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    ingestor = PublicationsIngestor(
        db_path=DB_PATH,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    ingestor.run()


if __name__ == "__main__":
    main()
