"""Снятие снепшота графа: Neo4j -> плоские структуры -> файл на диске.

Единственное место в цепочке `pauk/gui/`, которое реально ходит в Neo4j —
всё остальное (`generate_data.py`, `generate_stats.py` кроме `/api/stats`)
читает уже снятый снепшот с диска, не базу напрямую.

Слайс 1 этой переписки чинит конкретный баг: `load_db()` в `pauk/cache/`
фильтрует персон по метке `MATCH (p:Person:Itmo)`. Эта метка — наследие до
миграции на булево свойство `is_itmo` (см. `pauk/graph/client.py`,
`pauk/graph/jsonl_loader.py` — там она уже нигде не проставляется, персона
всегда несёт только `:Person` + свойство `is_itmo`). Существующие узлы,
загруженные до миграции, метку по инерции сохраняют, поэтому старый запрос
пока ещё что-то находит — но любой автор, добавленный в граф после миграции,
для него уже невидим, без единой ошибки: `MATCH` просто ничего не находит.

Здесь запрос переведён на фильтр `{is_itmo: true}` вместо метки — сам баг
исправлен, а область выборки сознательно остаётся прежней: только ИТМО-
персоны. Внешние (не-ИТМО) соавторы — `(:Person {is_itmo: false})` — по
графовой модели тоже пишут статьи, но их подключение к `load_db()` пока
осознанно отложено: это отдельное решение с последствиями для
`generate_data.py` (публикация без ни одного ИТМО-автора сейчас выпадает
из графа целиком), а не просто ещё один Cypher-фильтр.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from pauk.settings import Settings

from .graph_snapshot import write_snapshot

logger = logging.getLogger(__name__)

CYPHER_RETRIES = 5
"""Сколько раз повторить запрос при временном сбое связи с Neo4j, прежде чем
сдаться и пробросить исключение дальше."""


def _execute_retrying(driver, query, **params):
    """Выполняет один Cypher-запрос с повторами при временных сбоях сети.

    Транзиентные ошибки Neo4j-кластера (перевыбор лидера, обрыв сессии,
    временная недоступность) — это норма при долгом экспорте из десяти
    запросов подряд, а не повод падать с первой же осечки. Пауза между
    попытками растёт линейно (`5, 10, 15, ...` секунд), но не больше 60 —
    чтобы не ждать без толку минутами на быстро отходящем сервисе.

    Аргументы:
        driver: Открытый драйвер Neo4j (`neo4j.Driver`).
        query: Текст Cypher-запроса.
        **params: Именованные параметры запроса, передаются как есть в
            `driver.execute_query`.

    Возвращает:
        Список `neo4j.Record` — сырые строки результата, ещё не превращённые
        ни в кортежи (`cypher()`), ни в словари (`cypher_dict()`).

    Исключения:
        ServiceUnavailable | SessionExpired | TransientError | OSError:
            если сбой повторяется `CYPHER_RETRIES` раз подряд без единой
            успешной попытки.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            t0 = time.time()
            records, _, _ = driver.execute_query(query, **params)
            logger.info(
                "  %d   %.1f c: %s…",
                len(records),
                time.time() - t0,
                query.lstrip()[:60],
            )
            return records
        except (ServiceUnavailable, SessionExpired, TransientError, OSError) as exc:
            if attempt == CYPHER_RETRIES:
                raise
            wait = min(60, 5 * attempt)
            logger.warning(
                "  (%s: %s),  %d/%d,  %d c",
                type(exc).__name__,
                exc,
                attempt,
                CYPHER_RETRIES,
                wait,
            )
            time.sleep(wait)


def cypher(driver, query, **params) -> list[tuple]:
    """Запрос с повторами, строки — как позиционные кортежи.

    Подходит для таблиц со стабильной, заранее известной формой колонок
    (`publications`, `repositories`, ...) — вызывающий код распаковывает
    кортеж позиционно (`for pid, title, journal, ... in rows`), и лишняя
    колонка молча сдвинула бы всю распаковку. Для таблиц, чья форма
    ожидаемо растёт, есть `cypher_dict()`.

    Аргументы:
        driver: Открытый драйвер Neo4j.
        query: Текст Cypher-запроса. Порядок колонок в `RETURN` — это и есть
            порядок значений в каждом возвращаемом кортеже.
        **params: Именованные параметры запроса.

    Возвращает:
        Список кортежей — по одному на строку результата, в порядке колонок
        `RETURN`.

    Пример:
        >>> cypher(driver, "MATCH (p:Publication) RETURN p.id AS id, p.year AS year")
        [('P1', 2024), ('P2', None)]
    """
    return [tuple(r.values()) for r in _execute_retrying(driver, query, **params)]


def cypher_dict(driver, query, **params) -> list[dict]:
    """Запрос с повторами, строки — как словари по именам колонок Cypher.

    Используется там, где форма строки ожидаемо растёт со временем
    (`persons` — самая "толстая" и часто пополняемая таблица): добавление
    новой колонки в `RETURN` не требует правки позиционной распаковки нигде
    в вызывающем коде, в отличие от `cypher()`.

    Аргументы:
        driver: Открытый драйвер Neo4j.
        query: Текст Cypher-запроса.
        **params: Именованные параметры запроса.

    Возвращает:
        Список словарей — по одному на строку результата, ключи — алиасы
        колонок из `RETURN`.

    Пример:
        >>> cypher_dict(driver, "MATCH (p:Person {is_itmo: true}) RETURN p.id AS id, p.name_ru AS name_ru")
        [{'id': 'A1', 'name_ru': 'Иванов'}]
    """
    return [r.data() for r in _execute_retrying(driver, query, **params)]


def load_db(driver) -> dict[str, list]:
    """Читает весь граф в плоские структуры, которые ждёт `build_graph_data()`.

    Департаменты авторов и владельцы репозиториев — не плоские колонки в
    графовой модели, а связи (`BELONGS_TO`, `OWNED_BY`), поэтому здесь они
    достаются отдельными запросами через `OPTIONAL MATCH`.

    `persons` больше не фильтруется по устаревшей метке `:Itmo` — вместо неё
    везде используется свойство `{is_itmo: true}` (см. модульный докстринг
    выше про причину). Область выборки при этом не изменилась: как и
    раньше, здесь только ИТМО-персоны — `authorship`, `person_depts` и
    `repo_persons` фильтруют так же.

    Аргументы:
        driver: Открытый драйвер Neo4j.

    Возвращает:
        Плоский словарь из десяти ключей: `persons`/`publications`/
        `repositories`/`departments`/`authorship`/`person_depts`/
        `pub_depts`/`repo_pubs`/`repo_persons`/`repo_depts` — ровно то, что
        ожидает на входе `pauk/gui/generate_data.py::build_graph_data()`.
    """
    db: dict[str, list] = {}

    db["persons"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true}) "
        "RETURN p.id AS id, "
        "       p.first_name_ru AS first_name_ru, "
        "       p.second_name_ru AS second_name_ru, p.surname_ru AS surname_ru, "
        "       p.first_name_en AS first_name_en, p.second_name_en AS second_name_en, "
        "       p.surname_en AS surname_en, "
        "       p.name_ru AS name_ru, p.name_variants AS name_variants, "
        "       p.degree AS degree, p.github AS github, "
        "       p.orcid AS orcid",
    )

    db["publications"] = cypher(
        driver,
        "MATCH (pub:Publication) "
        "RETURN pub.id AS id, pub.title AS title, pub.journal AS journal, "
        "       pub.doi AS doi, toString(pub.publication_date) AS publication_date, "
        "       pub.year AS year, pub.has_code AS has_code, pub.code_url AS code_url",
    )

    db["repositories"] = cypher(
        driver,
        "MATCH (r:Repository) "
        "OPTIONAL MATCH (r)-[:OWNED_BY]->(gh:GitHubProfile) "
        "RETURN r.id AS id, r.name AS name, r.url AS url, "
        "       r.description AS description, r.stars_num AS stars_num, gh.login AS owner",
    )

    db["departments"] = cypher_dict(
        driver,
        "MATCH (d:Department) RETURN d.id AS id, d.name_ru AS name_ru, d.name_en AS name_en",
    )

    db["authorship"] = cypher(
        driver,
        "MATCH (p:Person {is_itmo: true})-[:AUTHORED]->(pub:Publication) "
        "RETURN pub.id AS pid, p.id AS per",
    )

    db["person_depts"] = cypher(
        driver,
        "MATCH (p:Person {is_itmo: true})-[:BELONGS_TO]->(d:Department) RETURN p.id AS per, d.id AS did",
    )

    db["pub_depts"] = cypher(
        driver,
        "MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) RETURN pub.id AS pid, d.id AS did ORDER BY d.id",
    )

    db["repo_pubs"] = cypher(
        driver,
        "MATCH (r:Repository)-[:IMPLEMENTS]->(pub:Publication) RETURN r.id AS rid, pub.id AS pid",
    )

    db["repo_persons"] = cypher(
        driver,
        "MATCH (p:Person {is_itmo: true})-[rel:CONTRIBUTED_TO]->(r:Repository) "
        "RETURN r.id AS rid, p.id AS per, rel.role AS role",
    )

    db["repo_depts"] = cypher(
        driver,
        "MATCH (r:Repository)-[:DEVELOPED_BY]->(d:Department) RETURN r.id AS rid, d.id AS did ORDER BY d.id",
    )

    return db


class GraphSnapshotExporter:
    """Точка входа команды `pauk cache export`: Neo4j -> файл-снепшот на диске."""

    def __init__(self, config: Settings) -> None:
        """Сохраняет конфигурацию подключения — сам драйвер открывается только в `export()`.

        Аргументы:
            config: Настройки проекта, в частности `neo4j_uri`/`neo4j_user`/
                `neo4j_password` и `cache_dir` (путь по умолчанию для снепшота).
        """
        self.config = config

    def export(self, path: Path | None = None) -> Path:
        """Снимает граф из Neo4j и атомарно пишет его снепшотом на диск.

        Аргументы:
            path: Куда писать снепшот. По умолчанию — `<cache_dir>/graph_snapshot.json`.

        Возвращает:
            Итоговый путь до записанного файла.

        Исключения:
            ValueError: пароль Neo4j не задан (`NEO4J_PASSWORD` пуст) — эта
                проверка нарочно стоит до открытия драйвера, чтобы получить
                понятную ошибку сразу, а не позднюю ошибку аутентификации от
                самого драйвера при первом запросе.
        """
        if not self.config.neo4j_password:
            raise ValueError("Neo4j password is empty - set NEO4J_PASSWORD in .env")

        target = path or self.config.cache_dir / "graph_snapshot.json"
        driver = GraphDatabase.driver(
            self.config.neo4j_uri,
            auth=(self.config.neo4j_user, self.config.neo4j_password),
        )
        try:
            driver.verify_connectivity()
            write_snapshot(target, load_db(driver))
        finally:
            driver.close()
        return target
