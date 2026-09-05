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

У каждого поля ниже, прямо в `load_db()`, — комментарий с аргументом
"оставить"/"не оставлять": это не решение автора кода, а материал для
вашего собственного решения — уберите или закомментируйте строку с полем,
которое не нужно. Часть аргументов повторяется дословно для целой группы
полей (все заглушки #152 — один и тот же аргумент: проверено грепом по
`pauk/pipeline/`/`pauk/sources/`, нигде не присваивается, значит на графе
это всегда `null`) — это не лень, а честное отражение того, что у всей
группы одна и та же причина.

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

Не включено даже сейчас: рекурсивная иерархия департаментов (`PART_OF`) и
`GitHubProfile`/`MENTIONS_LINK`/`LinkCandidate` как отдельные таблицы —
это не "ещё одно поле в RETURN", а структурно другая задача (обход дерева,
новые сущности верхнего уровня), заявленная отдельным следующим шагом.
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
        Плоский словарь из десяти ключей: `persons`/`publications`/
        `repositories`/`departments`/`authorship`/`person_depts`/
        `pub_depts`/`repo_pubs`/`repo_persons`/`repo_depts` — ровно то, что
        ожидает на входе `pauk/gui/generate_data.py::build_graph_data()`
        (сегодня — с поправкой на новые поля, которых там раньше не было).
    """
    db: dict[str, list] = {}

    # Все ИТМО-персоны: ФИО во всех вариантах, внешние профили, JSON-поля,
    # служебные метки Neo4j. Включает email/emails и все заглушки #152 -
    # решение по каждому полю ниже, отдельным комментарием.
    db["persons"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true}) "
        "RETURN "
        "p.id AS id, "  # обязателен, без него нет строки
        "p.openalex_id AS openalex_id, "  # внешний ключ на OpenAlex, дёшево, полезен для сверки. ОСТАВИТЬ
        "p.first_name_ru AS first_name_ru, "  # уже было в pauk/cache/. ОСТАВИТЬ
        "p.second_name_ru AS second_name_ru, "  # уже было. ОСТАВИТЬ
        "p.surname_ru AS surname_ru, "  # уже было. ОСТАВИТЬ
        "p.first_name_en AS first_name_en, "  # уже было. ОСТАВИТЬ
        "p.second_name_en AS second_name_en, "  # уже было. ОСТАВИТЬ
        "p.surname_en AS surname_en, "  # уже было. ОСТАВИТЬ
        "p.name_ru AS name_ru, "  # готовое полное имя (LLM-разбор), уже было. ОСТАВИТЬ
        "p.name_en AS name_en, "  # то же на английском. ОСТАВИТЬ
        "p.name_variants AS name_variants, "  # уже использовалось в new_gui. ОСТАВИТЬ
        "p.other_names AS other_names, "  # из ORCID, реальный источник. ОСТАВИТЬ - но пересекается по смыслу с name_variants, решить на выходе
        "p.degree AS degree, "  # уже было. ОСТАВИТЬ
        "p.github AS github, "  # уже было. ОСТАВИТЬ
        "p.orcid AS orcid, "  # уже было. ОСТАВИТЬ
        "p.google_scholar AS google_scholar, "  # живой профиль, дёшево. ОСТАВИТЬ на будущее - в new_gui пока некуда показать
        "p.openreview AS openreview, "  # аналогично google_scholar. ОСТАВИТЬ на будущее
        "p.email AS email, "  # ЛИЧНЫЕ ДАННЫЕ. ПРОТИВОРЕЧИЕ: neo4j-graph.md - "не публикуется", extract.py - пишет. РЕШИТЬ ОТДЕЛЬНО
        "p.emails AS emails, "  # ЛИЧНЫЕ ДАННЫЕ, рабочие данные github_match, не факт профиля. То же противоречие, что и email. РЕШИТЬ ОТДЕЛЬНО
        "p.thesis AS thesis, "  # ПРОВЕРЕНО: нигде не присваивается в pauk/pipeline или pauk/sources - на графе всегда null
        "p.scopus_id AS scopus_id, "  # СТАБ #152: источник не подключён, всегда null сегодня
        "p.researcher_id AS researcher_id, "  # СТАБ #152: источник не подключён, всегда null
        "p.dblp_id AS dblp_id, "  # СТАБ #152: источник не подключён, всегда null
        "p.biography AS biography, "  # СТАБ #152: источник не подключён, всегда null
        "p.country AS country, "  # СТАБ #152: источник не подключён, всегда null
        "p.homepage AS homepage, "  # СТАБ #152: источник не подключён, всегда null
        "p.gitlab_username AS gitlab_username, "  # СТАБ #152: источник не подключён, всегда null
        "p.linkedin AS linkedin, "  # СТАБ #152: источник не подключён, всегда null
        "p.twitter AS twitter, "  # СТАБ #152: источник не подключён, всегда null
        "p.wikipedia AS wikipedia, "  # СТАБ #152: источник не подключён, всегда null
        "p.works_count AS works_count, "  # СТАБ #152: источник не подключён, всегда null
        "p.cited_by_count AS cited_by_count, "  # СТАБ #152: источник не подключён, всегда null
        "p.h_index AS h_index, "  # СТАБ #152: источник не подключён, всегда null
        "p.i10_index AS i10_index, "  # СТАБ #152: источник не подключён, всегда null
        "p.counts_by_year AS counts_by_year, "  # СТАБ #152 (JSON-строка, если появится): источник не подключён
        "p.status AS status, "  # СТАБ #152: источник не подключён, всегда null
        "p.enriched_at AS enriched_at, "  # СТАБ #152 (поле модели, не путать с Neo4j updated_at ниже): всегда null
        "p.affiliations AS affiliations, "  # JSON-строка (места работы), реальный источник. ОСТАВИТЬ как есть
        "p.merged_ids AS merged_ids, "  # служебное, для дедупа/аудита, не для отображения. ОСТАВИТЬ если полезно внутренним инструментам
        "toString(p.created_at) AS created_at, "  # метка Neo4j (не модели): когда узел впервые записан. ОСТАВИТЬ
        "toString(p.updated_at) AS updated_at",  # метка Neo4j: когда узел последний раз тронут записью. ОСТАВИТЬ
    )

    # Все публикации: базовые метаданные + расширенные поля (funding,
    # abstract, versions, ...) и служебные метки Neo4j.
    db["publications"] = cypher_dict(
        driver,
        "MATCH (pub:Publication) "
        "RETURN "
        "pub.id AS id, "  # обязателен
        "pub.title AS title, "  # уже было. ОСТАВИТЬ
        "pub.type AS type, "  # тип работы (article/preprint/software/dataset,...), влияет на old_gui. ОСТАВИТЬ
        "pub.fields AS fields, "  # предметные области (список строк), полезно для группировки/similarity. ОСТАВИТЬ
        "pub.journal AS journal, "  # уже было. ОСТАВИТЬ
        "pub.doi AS doi, "  # уже было. ОСТАВИТЬ
        "toString(pub.publication_date) AS publication_date, "  # уже было (toString - Neo4j Date не JSON-сериализуем). ОСТАВИТЬ
        "pub.year AS year, "  # уже было. ОСТАВИТЬ
        "pub.has_code AS has_code, "  # уже было. ОСТАВИТЬ
        "pub.code_url AS code_url, "  # уже было. ОСТАВИТЬ
        "pub.funding AS funding, "  # JSON-строка (список грантов), реальный источник. ОСТАВИТЬ как есть
        "pub.openalex_url AS openalex_url, "  # прямая ссылка на источник, дёшево. ОСТАВИТЬ
        "pub.pdf_url AS pdf_url, "  # ссылка на PDF. ОСТАВИТЬ
        "pub.abstract AS abstract, "  # короткий текст, пригодится для будущего similarity-алгоритма. ОСТАВИТЬ
        "pub.full_text AS full_text, "  # ВНИМАНИЕ: весь текст статьи целиком, на порядки больше любого другого поля - раздует снепшот
        "pub.versions AS versions, "  # JSON-строка, история слияния дублей при дедупе. Техническое, редко нужно в UI
        "pub.merged_ids AS merged_ids, "  # служебное, для дедупа
        "toString(pub.created_at) AS created_at, "  # метка Neo4j. ОСТАВИТЬ
        "toString(pub.updated_at) AS updated_at",  # метка Neo4j. ОСТАВИТЬ
    )

    # Все репозитории + владелец (GitHubProfile.login через OWNED_BY).
    # Остальные поля самого GitHubProfile (name/company/location/...) - в
    # отдельную таблицу, следующим шагом, не здесь.
    db["repositories"] = cypher_dict(
        driver,
        "MATCH (r:Repository) "
        "OPTIONAL MATCH (r)-[:OWNED_BY]->(gh:GitHubProfile) "
        "RETURN "
        "r.id AS id, "  # обязателен
        "r.name AS name, "  # уже было. ОСТАВИТЬ
        "r.url AS url, "  # уже было. ОСТАВИТЬ
        "r.github_id AS github_id, "  # стабильный числовой id, переживает переименования - полезен для дедупа. ОСТАВИТЬ
        "r.description AS description, "  # уже было. ОСТАВИТЬ
        "r.cited_urls AS cited_urls, "  # URL, которыми на репо ссылались до канонизации - техническое, редко нужно в UI
        "r.stars_num AS stars_num, "  # уже было. ОСТАВИТЬ
        "toString(r.access_date) AS access_date, "  # когда данные о репо последний раз забирались с GitHub. ОСТАВИТЬ
        "r.has_readme AS has_readme, "  # дёшево, может влиять на отображение (например бейдж). ОСТАВИТЬ
        "toString(r.last_updated) AS last_updated, "  # дата последнего изменения на GitHub, полезно для сортировки. ОСТАВИТЬ
        "r.license AS license, "  # полезно для отображения. ОСТАВИТЬ
        "r.contributors AS contributors, "  # логины с самого GitHub API - ОСТОРОЖНО, это не то же самое, что repo_persons/CONTRIBUTED_TO
        "r.merged_ids AS merged_ids, "  # служебное, для дедупа
        "gh.login AS owner, "  # уже было. ОСТАВИТЬ
        "toString(r.created_at) AS created_at, "  # метка Neo4j. ОСТАВИТЬ
        "toString(r.updated_at) AS updated_at",  # метка Neo4j. ОСТАВИТЬ
    )

    # Департаменты: имена + поля-алиасы для полнотекстового сопоставления.
    # Рекурсивная иерархия PART_OF (кафедра -> факультет -> организация)
    # сюда не входит - это отдельный шаг (обход дерева, не колонка RETURN).
    db["departments"] = cypher_dict(
        driver,
        "MATCH (d:Department) "
        "RETURN "
        "d.id AS id, "  # обязателен
        "d.name_ru AS name_ru, "  # уже было. ОСТАВИТЬ
        "d.name_en AS name_en, "  # уже было. ОСТАВИТЬ
        "d.name_variants AS name_variants, "  # варианты написания названия. ОСТАВИТЬ, полезно для поиска
        "d.context_aliases AS context_aliases, "  # алиасы для матчинга по тексту публикаций. ОСТАВИТЬ, если используется matching-логикой
        "d.kind AS kind",  # тип юнита (кафедра/факультет/...). ОСТАВИТЬ, полезно для группировки в UI
    )

    # Кто что написал (AUTHORED) - только ИТМО-персоны (см. докстринг
    # выше), со свойствами самой связи: позиция в списке авторов,
    # аффилиация на момент публикации, признак корреспондирующего автора.
    db["authorship"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[rel:AUTHORED]->(pub:Publication) "
        "RETURN "
        "pub.id AS pid, "  # обязателен
        "p.id AS per, "  # обязателен
        "rel.position AS position, "  # порядковый номер автора в списке. ОСТАВИТЬ
        "rel.affiliation AS affiliation, "  # аффилиация на момент публикации (не текущая!). ОСТАВИТЬ
        "rel.affiliation_source AS affiliation_source, "  # откуда взята аффилиация (OpenAlex/ORCID/...). ОСТАВИТЬ
        "rel.is_corresponding AS is_corresponding",  # признак корреспондирующего автора. ОСТАВИТЬ
    )

    # Департамент каждой ИТМО-персоны (BELONGS_TO). У связи нет
    # собственных свойств - пара id, добавлять нечего.
    db["person_depts"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[:BELONGS_TO]->(d:Department) "
        "RETURN "
        "p.id AS per, "
        "d.id AS did",
    )

    # Департамент, "выпустивший" публикацию (PRODUCED_BY). У связи нет
    # собственных свойств - пара id, добавлять нечего.
    db["pub_depts"] = cypher_dict(
        driver,
        "MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) "
        "RETURN "
        "pub.id AS pid, "
        "d.id AS did "
        "ORDER BY d.id",
    )

    # Какой репозиторий реализует какую публикацию (IMPLEMENTS) - это
    # подтверждённая связь, не кандидат (кандидаты - MENTIONS_LINK, в
    # граф пока не читаются, см. модульный докстринг). У связи нет
    # собственных свойств - пара id, добавлять нечего.
    db["repo_pubs"] = cypher_dict(
        driver,
        "MATCH (r:Repository)-[:IMPLEMENTS]->(pub:Publication) RETURN r.id AS rid, pub.id AS pid",
    )

    # Кто из ИТМО-персон работал над репозиторием (CONTRIBUTED_TO), с
    # ролью (owner/contributor) - единственное собственное свойство связи.
    db["repo_persons"] = cypher_dict(
        driver,
        "MATCH (p:Person {is_itmo: true})-[rel:CONTRIBUTED_TO]->(r:Repository) "
        "RETURN "
        "r.id AS rid, "
        "p.id AS per, "
        "rel.role AS role",
    )

    # Департамент, "разработавший" репозиторий (DEVELOPED_BY). У связи
    # нет собственных свойств - пара id, добавлять нечего.
    db["repo_depts"] = cypher_dict(
        driver,
        "MATCH (r:Repository)-[:DEVELOPED_BY]->(d:Department)"
        "RETURN "
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
