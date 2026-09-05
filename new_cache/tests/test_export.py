"""Юнит-тесты для `export.py` — форма результата без обращения к реальной сети.

Ловят опечатки в алиасах `RETURN` и в сборке словаря `load_db()`, а также
служат регрессионным тестом на сам баг слайса 1 (метка `:Itmo` вместо
свойства `is_itmo`). Того, что сам текст Cypher структурно соответствует
реальной графовой модели (существуют ли такие метки/рёбра вообще), эти
тесты не проверяют в принципе — это задача `test_integration.py` с
одноразовым Neo4j-контейнером.
"""

from __future__ import annotations

import unittest
from unittest import mock

from neo4j.exceptions import ServiceUnavailable

from new_cache.export import GraphSnapshotExporter, cypher, cypher_dict, load_db
from pauk.settings import Settings


class FakeRecord:
    """Минимальная замена `neo4j.Record` — только то, что использует `cypher()`/`cypher_dict()`."""

    def __init__(self, mapping: dict):
        self._mapping = mapping

    def values(self):
        return tuple(self._mapping.values())

    def data(self):
        return dict(self._mapping)


class SequentialFakeDriver:
    """Драйвер-заглушка без сети: `execute_query()` по очереди отдаёт заранее
    заготовленные строки, по одному списку на вызов.

    `load_db()` всегда выполняет свои десять запросов в одном и том же
    порядке (persons, publications, ...) — позиционная очередь ответов
    надёжнее, чем сопоставление по тексту запроса, и не ломается от
    косметических правок форматирования Cypher.
    """

    def __init__(self, responses: list[list[dict]]):
        self._responses = iter(responses)
        self.queries: list[str] = []

    def execute_query(self, query, **params):
        self.queries.append(query)
        try:
            rows = next(self._responses)
        except StopIteration as exc:
            raise AssertionError(f"больше запросов, чем заготовленных ответов: {query!r}") from exc
        return [FakeRecord(r) for r in rows], None, None


class CypherHelpersTest(unittest.TestCase):
    def test_cypher_returns_positional_tuples_in_return_order(self):
        driver = SequentialFakeDriver([[{"id": "P1", "year": 2024}]])
        rows = cypher(driver, "MATCH (pub:Publication) RETURN pub.id AS id, pub.year AS year")
        self.assertEqual(rows, [("P1", 2024)])

    def test_cypher_dict_returns_column_keyed_dicts(self):
        driver = SequentialFakeDriver([[{"id": "A1", "name_ru": "Иванов"}]])
        rows = cypher_dict(driver, "MATCH (p:Person) RETURN p.id AS id, p.name_ru AS name_ru")
        self.assertEqual(rows, [{"id": "A1", "name_ru": "Иванов"}])


class ExecuteRetryingTest(unittest.TestCase):
    def test_retries_once_on_transient_error_then_succeeds(self):
        """Первая попытка падает с ServiceUnavailable, вторая — успешна.
        `time.sleep` подменяется, чтобы тест не занимал реальные секунды."""
        attempts = {"n": 0}

        class FlakyDriver:
            def execute_query(self, query, **params):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise ServiceUnavailable("временно недоступна")
                return [FakeRecord({"id": "ok"})], None, None

        with mock.patch("new_cache.export.time.sleep") as sleep_mock:
            rows = cypher(FlakyDriver(), "MATCH (n) RETURN n.id AS id")

        self.assertEqual(rows, [("ok",)])
        self.assertEqual(attempts["n"], 2)
        sleep_mock.assert_called_once()

    def test_gives_up_after_cypher_retries_attempts(self):
        class AlwaysFailingDriver:
            def execute_query(self, query, **params):
                raise ServiceUnavailable("недоступна")

        with mock.patch("new_cache.export.time.sleep"), self.assertRaises(ServiceUnavailable):
            cypher(AlwaysFailingDriver(), "MATCH (n) RETURN n.id AS id")


class LoadDbShapeTest(unittest.TestCase):
    """Проверяет, что load_db() раскладывает десять запросов по нужным ключам
    словаря — форма, которую дальше ожидает build_graph_data()."""

    @staticmethod
    def _empty_responses(count: int = 10) -> list[list[dict]]:
        return [[] for _ in range(count)]

    def test_persons_row_shape(self):
        responses = self._empty_responses()
        responses[0] = [{"id": "A1", "name_ru": "Иванов"}]
        db = load_db(SequentialFakeDriver(responses))
        self.assertEqual(db["persons"], [{"id": "A1", "name_ru": "Иванов"}])

    def test_result_has_all_ten_expected_keys(self):
        db = load_db(SequentialFakeDriver(self._empty_responses()))
        self.assertEqual(
            set(db.keys()),
            {
                "persons",
                "publications",
                "repositories",
                "departments",
                "authorship",
                "person_depts",
                "pub_depts",
                "repo_pubs",
                "repo_persons",
                "repo_depts",
            },
        )

    def test_person_related_queries_no_longer_filter_by_legacy_itmo_label(self):
        """Регрессионный тест на сам баг слайса 1: `:Person:Itmo` — снятая
        метка, её больше никто не проставляет (см. pauk/graph/jsonl_loader.py).
        Область выборки осталась прежней (только ИТМО-персоны, внешние
        соавторы пока сознательно не тянутся) — фильтр просто переехал
        с метки на свойство `is_itmo`."""
        driver = SequentialFakeDriver(self._empty_responses())
        load_db(driver)
        combined = " ".join(driver.queries)
        self.assertNotIn(":Itmo", combined)
        self.assertIn("{is_itmo: true}", combined)


class GraphSnapshotExporterTest(unittest.TestCase):
    def test_export_rejects_empty_neo4j_password(self):
        """Проверка стоит до открытия драйвера — понятная ошибка сразу,
        а не поздний сбой аутентификации внутри самого драйвера."""
        config = Settings(neo4j_password="")
        with self.assertRaises(ValueError):
            GraphSnapshotExporter(config).export()
