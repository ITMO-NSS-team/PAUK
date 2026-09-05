"""Юнит-тесты для `export.py` — форма результата без обращения к реальной сети.

Ловят опечатки в алиасах `RETURN` и в сборке словаря `load_db()`, а также
служат регрессионным тестом на сам баг слайса 1 (метка `:Itmo` вместо
свойства `is_itmo`). Того, что сам текст Cypher структурно соответствует
реальной графовой модели (существуют ли такие метки/рёбра/свойства вообще),
эти тесты не проверяют в принципе — по решению этого проекта здесь только
юнит-тесты с драйвером-заглушкой, без интеграционных тестов на реальном
Neo4j (см. память проекта: осознанный выбор в пользу простоты и скорости).
"""

from __future__ import annotations

import unittest
from unittest import mock

from neo4j.exceptions import ServiceUnavailable

from new_cache.export import GraphSnapshotExporter, cypher_dict, load_db
from pauk.settings import Settings


class FakeRecord:
    """Минимальная замена `neo4j.Record` — только то, что использует `cypher_dict()`."""

    def __init__(self, mapping: dict):
        self._mapping = mapping

    def values(self):
        return tuple(self._mapping.values())

    def data(self):
        return dict(self._mapping)


class SequentialFakeDriver:
    """Драйвер-заглушка без сети: `execute_query()` по очереди отдаёт заранее
    заготовленные строки, по одному списку на вызов.

    `load_db()` всегда выполняет свои тринадцать запросов в одном и том же
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
            rows = cypher_dict(FlakyDriver(), "MATCH (n) RETURN n.id AS id")

        self.assertEqual(rows, [{"id": "ok"}])
        self.assertEqual(attempts["n"], 2)
        sleep_mock.assert_called_once()

    def test_gives_up_after_cypher_retries_attempts(self):
        class AlwaysFailingDriver:
            def execute_query(self, query, **params):
                raise ServiceUnavailable("недоступна")

        with mock.patch("new_cache.export.time.sleep"), self.assertRaises(ServiceUnavailable):
            cypher_dict(AlwaysFailingDriver(), "MATCH (n) RETURN n.id AS id")


class LoadDbShapeTest(unittest.TestCase):
    """Проверяет, что load_db() раскладывает тринадцать запросов по нужным
    ключам словаря — форма, которую дальше ожидает build_graph_data() (плюс
    три новых таблицы, которых в build_graph_data() пока нет)."""

    @staticmethod
    def _empty_responses(count: int = 13) -> list[list[dict]]:
        return [[] for _ in range(count)]

    def test_persons_row_shape(self):
        responses = self._empty_responses()
        responses[0] = [{"id": "A1", "name_ru": "Иванов"}]
        db = load_db(SequentialFakeDriver(responses))
        self.assertEqual(db["persons"], [{"id": "A1", "name_ru": "Иванов"}])

    def test_publications_repositories_and_authorship_are_dict_shaped(self):
        """publications/repositories/authorship используют cypher_dict() —
        их форма растёт (много полей), и растущей форме нужны именованные
        словари, а не хрупкая позиционная распаковка."""
        responses = self._empty_responses()
        responses[1] = [{"id": "P1", "title": "Заголовок"}]
        responses[2] = [{"id": "R1", "name": "repo"}]
        responses[5] = [{"pid": "P1", "per": "A1", "position": 1}]
        db = load_db(SequentialFakeDriver(responses))
        self.assertEqual(db["publications"], [{"id": "P1", "title": "Заголовок"}])
        self.assertEqual(db["repositories"], [{"id": "R1", "name": "repo"}])
        self.assertEqual(db["authorship"], [{"pid": "P1", "per": "A1", "position": 1}])

    def test_new_slice_4_tables_are_dict_shaped(self):
        """organizations/mentions_repos/mentions_candidates и родительское
        поле departments - новые таблицы этого шага (GitHubProfile,
        MENTIONS_LINK/LinkCandidate, один шаг иерархии PART_OF)."""
        responses = self._empty_responses()
        responses[3] = [{"id": "D1", "parent_id": "O1", "parent_kind": "Organization"}]
        responses[4] = [{"id": "O1", "name_ru": "ИТМО"}]
        responses[9] = [{"pid": "P1", "rid": "R1", "is_relevant": True}]
        responses[10] = [{"pid": "P1", "candidate_id": "https://x", "url": "https://x"}]
        db = load_db(SequentialFakeDriver(responses))
        self.assertEqual(db["departments"], [{"id": "D1", "parent_id": "O1", "parent_kind": "Organization"}])
        self.assertEqual(db["organizations"], [{"id": "O1", "name_ru": "ИТМО"}])
        self.assertEqual(db["mentions_repos"], [{"pid": "P1", "rid": "R1", "is_relevant": True}])
        self.assertEqual(
            db["mentions_candidates"],
            [{"pid": "P1", "candidate_id": "https://x", "url": "https://x"}],
        )

    def test_debatable_fields_are_present_pending_manual_review(self):
        """По прямой просьбе запросы включают буквально все свойства из
        NODE_REGISTRY, в том числе те, что раньше были осознанно исключены
        (email/emails - открытое противоречие документации и extract.py;
        заглушки Person из #152, которые на графе сегодня всегда null;
        Publication.full_text - по размеру). У каждого поля в export.py
        рядом стоит комментарий "оставить"/аргумент против - решение,
        что вычеркнуть, за человеком, а не за этим тестом. Тест лишь
        фиксирует, что все эти поля сейчас реально запрашиваются."""
        driver = SequentialFakeDriver(self._empty_responses())
        load_db(driver)
        combined = " ".join(driver.queries)
        for included in (
            "p.email AS email",
            "p.emails AS emails",
            "pub.full_text AS full_text",
            "p.scopus_id AS scopus_id",
            "p.biography AS biography",
            "p.h_index AS h_index",
            "p.counts_by_year AS counts_by_year",
            "p.thesis AS thesis",
            "p.status AS status",
        ):
            self.assertIn(included, combined, f"поле {included!r} должно быть в запросе (удалите вручную, если не нужно)")

    def test_result_has_all_thirteen_expected_keys(self):
        db = load_db(SequentialFakeDriver(self._empty_responses()))
        self.assertEqual(
            set(db.keys()),
            {
                "persons",
                "publications",
                "repositories",
                "departments",
                "organizations",
                "authorship",
                "person_depts",
                "pub_depts",
                "repo_pubs",
                "mentions_repos",
                "mentions_candidates",
                "repo_persons",
                "repo_depts",
            },
        )

    def test_slice_4_capabilities_are_wired_into_the_actual_queries(self):
        """Не только форма результата (test_new_slice_4_tables_are_dict_shaped),
        но и то, что каждая новая возможность реально запрашивается через
        правильную связь графа - GitHubProfile через OWNED_BY, MENTIONS_LINK
        отдельно от IMPLEMENTS, PART_OF отдельно от BELONGS_TO."""
        driver = SequentialFakeDriver(self._empty_responses())
        load_db(driver)
        combined = " ".join(driver.queries)
        self.assertIn("gh.company AS owner_company", combined)
        self.assertIn("OPTIONAL MATCH (d)-[:PART_OF]->(parent)", combined)
        self.assertIn("labels(parent)[0] AS parent_kind", combined)
        self.assertIn("[rel:MENTIONS_LINK]->(r:Repository)", combined)
        self.assertIn("[rel:MENTIONS_LINK]->(lc:LinkCandidate)", combined)

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
