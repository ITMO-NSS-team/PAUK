from __future__ import annotations

import base64
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from pauk.search import batch
from pauk.search.entities import NameIndex, initials_compatible, name_key, transliterate
from pauk.search.llm import ANSWER_MAX_TOKENS, allowed_urls, compose_answer, strip_unknown_links
from pauk.search.plan import QueryPlan
from pauk.search.ranking import RankingConfig, position_weight, preset, rank, recency_weight, trim
from pauk.search.reader import WriteQueryRejected, assert_read_only
from pauk.search.retrieval import Collection, min_max, pool, tokenize
from pauk.sources.base import HttpRequestError
from pauk.sources.github import GitHubClient


class ReadOnlyGuardTest(unittest.TestCase):
    def test_reads_pass(self):
        for query in (
            "MATCH (p:Person) RETURN p.id",
            "MATCH (p) WHERE p.name = 'SET by hand' RETURN p",  # keyword inside a string literal
            "MATCH (p) RETURN p.offset AS created",  # keyword-like identifiers
            "CALL db.index.vector.queryNodes('x', 5, $v) YIELD node RETURN node",
        ):
            assert_read_only(query)

    def test_writes_rejected(self):
        for query in (
            "MATCH (p:Person) SET p.x = 1",
            "CREATE (n:Test)",
            "MERGE (n:Person {id: 'a'})",
            "MATCH (n) DETACH DELETE n",
            "MATCH (n) REMOVE n.x",
            "DROP INDEX foo",
            "CALL { MATCH (n) RETURN n } IN TRANSACTIONS",
        ):
            with self.assertRaises(WriteQueryRejected, msg=query):
                assert_read_only(query)


class RetrievalTest(unittest.TestCase):
    def setUp(self):
        vectors = np.array([[1, 0], [0.8, 0.6], [0, 1]], dtype=np.float32)
        self.collection = Collection(
            ["a", "b", "c"],
            ["diffusion models for images", "denoising generative models", "ion diffusion in crystals"],
            vectors,
        )
        self.embed = lambda phrases: np.array([[1, 0]], dtype=np.float32)

    def test_min_max_spreads_a_narrow_band(self):
        spread = min_max([("a", 0.64), ("b", 0.60), ("c", 0.52)])
        self.assertEqual((spread["a"], spread["c"]), (1.0, 0.0))
        self.assertAlmostEqual(spread["b"], 2 / 3)
        self.assertEqual(min_max([("a", 0.5)]), {"a": 1.0})

    def test_phrases_are_pooled_into_one_normalised_query(self):
        pooled = pool(np.array([[1, 0], [0, 1]], dtype=np.float32))
        self.assertEqual(pooled.shape, (1, 2))
        self.assertAlmostEqual(float(np.linalg.norm(pooled)), 1.0, places=6)

    def test_dense_orders_by_cosine_and_respects_allowed(self):
        hits = self.collection.search("dense", ["q"], self.embed, k=3)
        self.assertEqual([h.id for h in hits], ["a", "b", "c"])
        hits = self.collection.search("dense", ["q"], self.embed, k=3, allowed={"b", "c"})
        self.assertEqual([h.id for h in hits], ["b", "c"])

    def test_bm25_needs_word_overlap(self):
        hits = self.collection.search("bm25", ["diffusion"], None, k=3)
        self.assertEqual({h.id for h in hits}, {"a", "c"})
        self.assertEqual(max(h.score for h in hits), 1.0)

    def test_hybrid_scores_are_normalised(self):
        hits = self.collection.search("hybrid", ["diffusion"], self.embed, k=3)
        self.assertEqual(hits[0].score, 1.0)
        self.assertTrue(all(0 < h.score <= 1 for h in hits))

    def test_tokenize_folds_case_and_yo(self):
        self.assertEqual(tokenize("Ёлка и Diffusion-Models"), ["елка", "diffusion", "models"])


class EntitiesTest(unittest.TestCase):
    def setUp(self):
        self.index = NameIndex("person", [
            {"id": "A1", "name_en": "Ivan Ivanov", "name_ru": "Иван Иванов", "is_itmo": True, "publications": 12},
            {"id": "A2", "name_en": "Petr Ivanov", "name_ru": None, "is_itmo": True, "publications": 3},
            {"id": "A3", "name_en": "Ivan Ivanov", "name_ru": None, "is_itmo": False, "publications": 1},
            {"id": "A4", "name_en": "Dmitry Gilemkhanov", "name_variants": ["D. Gilemkhanov"], "is_itmo": True},
        ], ("name_en", "name_ru", "name_variants"))

    def test_transliteration_matches_openalex_spelling(self):
        self.assertEqual(name_key("Гилемханов Дмитрий"), name_key("Dmitry Gilemkhanov").split()[1] + " "
                         + name_key("Dmitry Gilemkhanov").split()[0])
        self.assertEqual(transliterate("Щукин"), "shchukin")

    def test_cyrillic_query_finds_latin_name_and_prefers_itmo(self):
        resolution = self.index.resolve("Иванов Иван")
        self.assertEqual(resolution.best.id, "A1")
        self.assertNotIn("A2", [c.id for c in resolution.candidates[:1]])

    def test_initials_filter_out_other_first_names(self):
        self.assertTrue(initials_compatible("ivanov i", "ivan ivanov"))
        self.assertFalse(initials_compatible("ivanov i", "petr ivanov"))
        ids = [c.id for c in self.index.resolve("Иванов И.").candidates]
        self.assertIn("A1", ids)
        self.assertNotIn("A2", ids)

    def test_unknown_name_resolves_to_nothing(self):
        self.assertIsNone(self.index.resolve("Совершенно Неизвестный").best)


class PlanTest(unittest.TestCase):
    def test_type_follows_contents(self):
        self.assertEqual(QueryPlan(core=["x"]).type, "topic")
        graph = QueryPlan(type="topic", entities=[{"kind": "person", "name": "Иванов"}])
        self.assertEqual((graph.type, graph.graph_type), ("graph", "person_profile"))
        mixed = QueryPlan(entities=[{"kind": "department", "name": "лаб"}], core=["x"])
        self.assertEqual((mixed.type, mixed.graph_type), ("mixed", "topic_subgraph"))

    def test_unknown_fields_are_ignored(self):
        plan = QueryPlan(core=["x"], fields=["name", "shoe_size"])
        self.assertEqual(plan.wanted_fields("persons"), {"name"})
        self.assertIsNone(plan.wanted_fields("publications"))


class AnswerLinksTest(unittest.TestCase):
    def test_invented_links_are_dropped(self):
        sections = {"persons": [{"label": "Иван", "url": "https://orcid.org/1",
                                 "repositories": ["https://github.com/a/b"]}]}
        allowed = allowed_urls(sections)
        text = "[Иван](https://orcid.org/1), [код](https://github.com/a/b), [фейк](https://evil.example/x)"
        self.assertEqual(strip_unknown_links(text, allowed),
                         "[Иван](https://orcid.org/1), [код](https://github.com/a/b), фейк")

    def test_answer_length_is_capped(self):
        class FakeClient:
            last_error = None

            def chat_json(self, prompt, *, system_prompt=None, max_tokens=None):
                self.max_tokens = max_tokens
                return {"answer": "ok"}

        client = FakeClient()
        self.assertEqual(compose_answer(client, "q", {}, []), "ok")
        self.assertEqual(client.max_tokens, ANSWER_MAX_TOKENS)


class ReadmeTest(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient(timeout=5)

    def test_readme_text_is_decoded(self):
        body = {"encoding": "base64", "content": base64.b64encode("# Привет\n".encode()).decode()}
        with patch.object(self.client, "get_json", return_value=body):
            self.assertEqual(self.client.get_readme("o", "r"), "# Привет\n")

    def test_no_readme_is_none_and_other_errors_propagate(self):
        with patch.object(self.client, "get_json", side_effect=HttpRequestError("GET", "u", status_code=404)):
            self.assertIsNone(self.client.get_readme("o", "r"))
        with patch.object(self.client, "get_json", side_effect=HttpRequestError("GET", "u", status_code=500)), \
                self.assertRaises(HttpRequestError):
            self.client.get_readme("o", "r")


def _subgraph():
    return {
        "publications": [
            {"id": "P1", "title": "Diffusion", "year": 2026, "departments": ["lab"]},
            {"id": "P2", "title": "Old diffusion", "year": 2014, "departments": ["lab", "inst"]},
            {"id": "P3", "title": "Unrelated", "year": 2025, "departments": []},
        ],
        "authorship": [
            {"pub": "P1", "person": "first", "position": 1, "n_authors": 3},
            {"pub": "P1", "person": "middle", "position": 2, "n_authors": 3},
            {"pub": "P1", "person": "external", "position": 3, "n_authors": 3},
            {"pub": "P2", "person": "middle", "position": 1, "n_authors": 1},
        ],
        "repositories": [
            {"id": "R1", "name": "repo", "url": "https://github.com/x/repo", "publications": ["P1"],
             "contributors": [{"person": "coder", "role": "owner"}]},
        ],
        "persons": {
            "first": {"id": "first", "name_ru": "Первый", "is_itmo": True, "departments": ["lab"]},
            "middle": {"id": "middle", "name_ru": "Средний", "is_itmo": True, "departments": []},
            "external": {"id": "external", "name_en": "External", "is_itmo": False, "departments": []},
            "coder": {"id": "coder", "name_ru": "Кодер", "is_itmo": True, "departments": []},
        },
        "departments": {
            "lab": {"id": "lab", "name_ru": "Лаборатория", "kind": "lab", "ancestors": [{"id": "inst", "depth": 1}]},
            "inst": {"id": "inst", "name_ru": "Институт", "kind": "institute", "ancestors": []},
        },
    }


class RankingTest(unittest.TestCase):
    def test_weights(self):
        config = RankingConfig(current_year=2026)
        self.assertEqual(position_weight(1, 5, config), 1.0)
        self.assertEqual(position_weight(5, 5, config), 1.0)
        self.assertEqual(position_weight(3, 5, config), 0.5)
        self.assertEqual(recency_weight(2026, config), 1.0)
        self.assertAlmostEqual(recency_weight(2022, config), 0.5)
        off = preset("authorship")
        self.assertEqual((position_weight(3, 5, off), recency_weight(2000, off)), (1.0, 1.0))

    def test_graph_ranking_uses_position_recency_code_and_hierarchy(self):
        plan = QueryPlan(core=["diffusion"])
        relevance = {"P1": 1.0, "P2": 1.0}
        sections = rank(_subgraph(), relevance, {}, plan, RankingConfig(current_year=2026))
        persons = [p["id"] for p in sections["persons"]]
        self.assertNotIn("external", persons)  # only_itmo by default
        self.assertEqual(persons[0], "first")
        self.assertIn("coder", persons)  # reached only through the repository
        departments = {d["id"]: d for d in sections["departments"]}
        self.assertEqual(set(departments), {"lab", "inst"})
        self.assertEqual(departments["inst"]["publications"], 2)
        # The lab did the work; its institute gets only the decayed share.
        self.assertGreater(departments["lab"]["score"], departments["inst"]["score"])

    def test_authorship_ranking_ignores_graph_structure(self):
        plan = QueryPlan(core=["diffusion"])
        sections = rank(_subgraph(), {"P1": 1.0, "P2": 1.0}, {}, plan, preset("authorship"))
        persons = [p["id"] for p in sections["persons"]]
        self.assertEqual(persons[0], "middle")  # two papers, no position or recency discount
        self.assertNotIn("coder", persons)
        self.assertEqual({d["id"] for d in sections["departments"]}, {"lab"})

    def test_year_filter_and_trim(self):
        plan = QueryPlan(core=["x"], filter={"year_from": 2020}, expected_values=["publications"],
                         fields=["title"])
        sections = rank(_subgraph(), {"P1": 1.0, "P2": 0.9}, {}, plan, RankingConfig(current_year=2026))
        self.assertEqual([p["id"] for p in sections["publications"]], ["P1"])
        trimmed = trim(sections, plan, top_n=5)
        self.assertEqual(list(trimmed), ["publications"])
        self.assertEqual(set(trimmed["publications"][0]), {"id", "label", "url", "title"})


class BatchTest(unittest.TestCase):
    class FakeEngine:
        def __init__(self):
            self.asked = []

        def ask(self, question, options):
            self.asked.append(question)
            if question == "сломай":
                raise RuntimeError("boom")
            return {"question": question, "plan": {"type": "topic", "core": [question]},
                    "sections": {"persons": [{"label": "Иван", "score": 0.5}]}, "entities": [],
                    "entry_points": [], "notes": [], "trace": {}, "timings": {"parse": 0.1}, "answer": None}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.out = self.dir / "out"

    def _review(self):
        with (self.out / "review.csv").open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_txt_and_csv_inputs(self):
        txt = self.dir / "q.txt"
        txt.write_text("# комментарий\nпервый\n\nвторой\nпервый\n", encoding="utf-8")
        self.assertEqual([q["question"] for q in batch.read_questions(txt)], ["первый", "второй"])
        table = self.dir / "q.csv"
        table.write_text("question,type\nкто X,topic\n", encoding="utf-8")
        self.assertEqual(batch.read_questions(table), [{"question": "кто X", "type": "topic"}])

    def test_answers_are_reused_and_marks_survive_a_rerun(self):
        questions = self.dir / "q.txt"
        questions.write_text("один\nсломай\n", encoding="utf-8")
        engine = self.FakeEngine()
        options = batch.AskOptions(llm_answer=False)
        summary = batch.run(questions, self.out, options, engine=engine)
        self.assertEqual((summary["questions"], summary["errors"]), (2, 1))
        rows = self._review()
        self.assertEqual(rows[0]["persons"], "Иван (0.50)")
        self.assertIn("boom", rows[1]["notes"])

        rows[0]["correct"] = "да"
        with (self.out / "review.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        questions.write_text("один\nсломай\nдва\n", encoding="utf-8")
        summary = batch.run(questions, self.out, options, engine=engine)
        # "один" is reused, the failed one is asked again, "два" is new.
        self.assertEqual(engine.asked, ["один", "сломай", "сломай", "два"])
        self.assertEqual(summary["reused"], 1)
        self.assertEqual(self._review()[0]["correct"], "да")


if __name__ == "__main__":
    unittest.main()
