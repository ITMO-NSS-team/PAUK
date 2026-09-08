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

Слайс 3 (эта версия): по прямой просьбе — везде один способ читать строки
(`cypher_dict()`, `cypher()` убран как отдельная функция за ненадобностью:
позиционные кортежи не давали ничего, кроме риска молча разъехаться при
следующей правке `RETURN`), и в каждый запрос включены буквально все
свойства, какие вообще существуют на узле/связи в графовой модели
(`pauk/graph/extract.py::NODE_REGISTRY` — единственный источник правды о
том, что физически пишется в узел), включая те, что предыдущая версия
этого файла сознательно исключала (`email`/`emails`, полтора десятка
заглушечных полей `Person` из #152, `Publication.full_text`).

Отбор полей под нужды веба (2026-09-08) уже сделан вручную, по одному —
закомментированные строки ниже это то, что решили не тащить на сайт вообще
(в основном заглушки #152, всегда `null` на графе — проверено грепом по
`pauk/pipeline/`/`pauk/sources/`). У каждого оставшегося (не закомментированного)
поля — комментарий "оба"/"public"/"private" с кратким обоснованием: значит
это поле дойдёт до веба, вопрос только в том, в какой из двух сборок
(`new_generate/graph_builder.py --public`/`--private`) оно должно попасть.

`created_at`/`updated_at` — это НЕ одноимённые (и не заполняемые) поля
Pydantic-моделей, а служебные метки времени, которые сам `Neo4jClient`
проставляет на КАЖДЫЙ узел при первой записи/любом обновлении
(`ON CREATE SET n.created_at = datetime()`, `ON MATCH SET n.updated_at =
datetime()` в `pauk/graph/client.py`) — они есть у любого узла независимо
от того, что происходит на уровне доменной модели. Neo4j возвращает их как
собственный тип `neo4j.time.DateTime`, который `json.dump()` не умеет
сериализовать напрямую, поэтому здесь они, как и `publication_date`,
достаются через `toString()` — уже готовой строкой.

`funding`/`versions`/`affiliations` при записи сериализуются в JSON-текст
(`pauk/graph/extract.py::JSON_TEXT_FIELDS` — Neo4j не хранит вложенные
map/list-of-map как есть). Здесь они читаются обратно как есть — JSON-
строка, не распарсенный объект: разбор — забота потребителя снепшота,
`load_db()` остаётся честным зеркалом того, что реально лежит на узле.

Слайс 4 (эта версия): добавлены `GitHubProfile` (поля владельца — прямо в
строку `repositories`, с префиксом `owner_`, т.к. связь 1:1 и отдельная
таблица под неё не нужна), `MENTIONS_LINK`/`LinkCandidate` (`mentions_repos`
и `mentions_candidates` — доказательная база кода-ссылок, отдельно от
подтверждённого `IMPLEMENTS`/`repo_pubs`), и ОДИН шаг иерархии департаментов
(`departments.parent_id`/`parent_kind` через `PART_OF`). Полная рекурсивная
цепочка (кафедра -> факультет -> ... -> организация) сюда всё ещё не
входит — обходить дерево внутри `load_db()` на каждый департамент не нужно:
у потребителя снепшота уже будут все пары (ребёнок, родитель) в одной
плоской таблице `departments`, и подняться по цепочке — это его работа в
Python, а не ещё один рекурсивный Cypher здесь.

`is_fresh()` (`graph_snapshot.py`) остаётся неподключённым сознательно: его
единственный осмысленный вызывающий — не что-то внутри `new_cache`, а
`pauk/gui/generate_data.py` на чтении снепшота ("предупредить, что данные
устарели, перед генерацией сайта") — а трогать `pauk/gui/` сейчас
осознанно не входит в объём этой работы (см. память проекта).

ВАЖНО НА БУДУЩЕЕ, до самого переключения `pauk/cache/` -> `new_cache/`:
`pauk/gui/generate_data.py::build_graph_data()` сегодня всё ещё написан под
СТАРУЮ, позиционно-кортежную форму `db` (`for pid, per in db["authorship"]`,
`r[0]` на строках `publications`/`repositories`, `for pid, did in
db["pub_depts"]` и т.д.) — после перехода этого модуля на `cypher_dict()`
эта форма ломается. Проверено не на словах: синтетический `db` в новой
форме, скормленный прямо в `build_graph_data()` без похода в Neo4j, даёт
`ValueError: too many values to unpack`. Само по себе переключение
`pauk/cache/` на новую форму не тянет `generate_data.py` за собой
автоматически — когда до переключения дойдёт дело, `generate_data.py`
нужно будет переписать под словарную форму отдельным шагом, не как
побочный эффект.
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

CYPHER_RETRY_BACKOFF_STEP_SECONDS = 5
"""Шаг линейного роста паузы между попытками (5, 10, 15, ... секунд)."""

CYPHER_RETRY_MAX_WAIT_SECONDS = 60
"""Верхний предел паузы между попытками — не ждать без толку минутами."""


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
        в словари (`cypher_dict()`).

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
            wait = min(CYPHER_RETRY_MAX_WAIT_SECONDS, CYPHER_RETRY_BACKOFF_STEP_SECONDS * attempt)
            logger.warning(
                "  (%s: %s),  %d/%d,  %d c",
                type(exc).__name__,
                exc,
                attempt,
                CYPHER_RETRIES,
                wait,
            )
            time.sleep(wait)


def cypher_dict(driver, query, **params) -> list[dict]:
    """Запрос с повторами, строки — как словари по именам колонок Cypher.

    Единственный способ читать строки в этом модуле (см. модульный
    докстринг выше про причину отказа от отдельной позиционно-кортежной
    версии): словарь по ключу не ломается от изменения порядка/состава
    колонок в `RETURN`, а кортеж — ломался бы молча.

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

    Полный список полей (в т.ч. тех, что стоит перепроверить/убрать) и
    аргументы по каждому — комментариями прямо у соответствующей строки
    `RETURN` ниже, а не в этом докстринге: так решение видно рядом с
    полем, к которому оно относится.

    Аргументы:
        driver: Открытый драйвер Neo4j.

    Возвращает:
        Плоский словарь из тринадцати ключей: `persons`/`publications`/
        `repositories`/`departments`/`organizations`/`authorship`/
        `person_depts`/`pub_depts`/`repo_pubs`/`mentions_repos`/
        `mentions_candidates`/`repo_persons`/`repo_depts`. Первые десять —
        то, что ожидает на входе `pauk/gui/generate_data.py::build_graph_data()`
        (сегодня — с поправкой на новые поля, которых там раньше не было);
        `organizations`/`mentions_repos`/`mentions_candidates` — три новых
        таблицы, у которых пока нет потребителя в существующем коде.
    """
    db: dict[str, list] = {}

    db["persons"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true}) "
        "RETURN "
        # required
        "p.id AS id, "
        # public
        "p.openalex_id AS openalex_id, "
        # private
        "p.name_ru AS name_ru, "
        "p.name_en AS name_en, "
        "p.name_variants AS name_variants, "
        "p.other_names AS other_names, "
        "p.degree AS degree, "
        "p.github AS github, "
        "p.orcid AS orcid, "
        "p.google_scholar AS google_scholar, "
        "p.openreview AS openreview, "
        "p.email AS email, "
        "p.emails AS emails, "
        "p.affiliations AS affiliations, "
        # both (split into public/private)
        "p.surname_ru AS surname_ru, "
        "p.first_name_ru AS first_name_ru, "
        "p.second_name_ru AS second_name_ru, "
        "p.surname_en AS surname_en, "
        "p.first_name_en AS first_name_en, "
        "p.second_name_en AS second_name_en, "
        # stubs
        # "p.thesis AS thesis, "  # STUB
        # "p.scopus_id AS scopus_id, "  # STUB
        # "p.researcher_id AS researcher_id, "  # STUB
        # "p.dblp_id AS dblp_id, "  # STUB
        # "p.biography AS biography, "  # STUB
        # "p.country AS country, "  # STUB
        # "p.homepage AS homepage, "  # STUB
        # "p.gitlab_username AS gitlab_username, "  # STUB
        # "p.linkedin AS linkedin, "  # STUB
        # "p.twitter AS twitter, "  # STUB
        # "p.wikipedia AS wikipedia, "  # STUB
        # "p.works_count AS works_count, "  # STUB
        # "p.cited_by_count AS cited_by_count, "  # STUB
        # "p.h_index AS h_index, "  # STUB
        # "p.i10_index AS i10_index, "  # STUB
        # "p.counts_by_year AS counts_by_year, "  # STUB
        # "p.status AS status, "  # STUB
        # "p.enriched_at AS enriched_at, "  # STUB
        # service
        "toString(p.created_at) AS created_at, "
        "toString(p.updated_at) AS updated_at",
    )

    db["publications"] = cypher_dict(
        driver,
        "MATCH (pub:Publication) "
        "RETURN "
        # required
        "pub.id AS id, "
        # public
        "pub.title AS title, "
        "pub.type AS type, "
        "pub.fields AS fields, "
        "pub.journal AS journal, "
        "pub.doi AS doi, "
        "pub.has_code AS has_code, "
        "pub.code_url AS code_url, "
        "pub.funding AS funding, "
        "pub.openalex_url AS openalex_url, "
        "pub.abstract AS abstract, "
        "pub.versions AS versions, "
        "pub.year AS year, "
        "toString(pub.publication_date) AS publication_date, "
        # service
        "toString(pub.created_at) AS created_at, "
        "toString(pub.updated_at) AS updated_at",
    )

    db["repositories"] = cypher_dict(
        driver,
        "MATCH (r:Repository) "
        "OPTIONAL MATCH (r)-[:OWNED_BY]->(gh:GitHubProfile) "
        "RETURN "
        # required
        "r.id AS id, "
        # public
        "r.name AS name, "
        "r.url AS url, "
        "r.description AS description, "
        "r.stars_num AS stars_num, "
        "r.has_readme AS has_readme, "
        "r.license AS license, "
        "r.contributors AS contributors, "
        # "gh.login AS owner, "
        # "gh.name AS owner_name, "  # TODO: decide is it necessary + why not nameS + classification by type
        # "gh.html_url AS owner_html_url, "
        # "gh.description AS owner_description, "
        # "gh.location AS owner_location, "
        # "gh.company AS owner_company, "
        "gh.type AS owner_type, "
        # service
        "toString(r.access_date) AS access_date, "
        # "toString(r.last_updated) AS last_updated, "  # STUB
        "toString(r.created_at) AS created_at, "
        "toString(r.updated_at) AS updated_at",
    )

    db["departments"] = cypher_dict(
        driver,
        "MATCH (d:Department) "
        "OPTIONAL MATCH (d)-[:PART_OF]->(parent) "
        "RETURN "
        # required
        "d.id AS id, "
        # public
        "d.name_ru AS name_ru, "
        "d.name_en AS name_en, "
        "d.name_variants AS name_variants, "
        "d.context_aliases AS context_aliases, "
        "d.kind AS kind, ",
        # "parent.id AS parent_id, "
        # "labels(parent)[0] AS parent_kind"
    )

    db["organizations"] = cypher_dict(
        driver,
        "MATCH (o:Organization) "
        "RETURN "
        # required
        "o.id AS id, "
        # public
        "o.name_ru AS name_ru, "
        "o.name_en AS name_en, "
        "o.ror_id AS ror_id, "
        "o.country AS country, "
        "o.type AS type",
    )

    db["authorship"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[rel:AUTHORED]->(pub:Publication) "
        "RETURN "
        # required
        "pub.id AS pid, "
        "p.id AS per, "
        # public
        "rel.position AS position, "
        "rel.is_corresponding AS is_corresponding",
    )

    db["person_depts"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[:BELONGS_TO]->(d:Department) "
        "RETURN "
        # required
        "p.id AS per, "
        "d.id AS did",
    )

    db["pub_depts"] = cypher_dict(
        driver,
        "MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) "
        "RETURN "
        # required
        "pub.id AS pid, "
        "d.id AS did "
        "ORDER BY d.id",
    )

    db["repo_pubs"] = cypher_dict(
        driver,
        "MATCH (r:Repository)-[:IMPLEMENTS]->(pub:Publication) "
        "RETURN "
        # required
        "r.id AS rid, "
        "pub.id AS pid, ",
    )

    db["mentions_repos"] = cypher_dict(
        driver,
        "MATCH (pub:Publication)-[rel:MENTIONS_LINK]->(r:Repository) "
        "RETURN "
        # required
        "pub.id AS pid, "
        "r.id AS rid, "
        # public
        "rel.is_relevant AS is_relevant, ",
    )

    db["mentions_candidates"] = cypher_dict(
        driver,
        "MATCH (pub:Publication)-[rel:MENTIONS_LINK]->(lc:LinkCandidate) "
        "RETURN "
        # required
        "pub.id AS pid, "
        # public
        "lc.url AS url, "
        "lc.host AS host, "
        "rel.is_relevant AS is_relevant, ",
    )

    db["repo_persons"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[rel:CONTRIBUTED_TO]->(r:Repository) "
        "RETURN "
        # required
        "r.id AS rid, "
        "p.id AS per, "
        # public
        "rel.role AS role",
    )

    db["repo_depts"] = cypher_dict(
        driver,
        "MATCH (r:Repository)-[:DEVELOPED_BY]->(d:Department) "
        "RETURN "
        # required
        "r.id AS rid, "
        "d.id AS did "
        "ORDER BY d.id",
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
