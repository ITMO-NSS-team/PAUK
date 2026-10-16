from __future__ import annotations

import base64
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from pauk.search import batch, evaluation
from pauk.search.entities import NameIndex, initials_compatible, name_key, transliterate
from pauk.search.index import is_searchable
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


class SearchableTest(unittest.TestCase):
    def test_figures_and_front_matter_are_not_searchable(self):
        self.assertFalse(is_searchable({"type": "other", "title": "Graphic Abstract", "abstract": None}))
        self.assertFalse(is_searchable({"type": "dataset", "title": "Сравнительные характеристики алгоритмов"}))
        self.assertFalse(is_searchable({"type": "paratext", "title": "Contributors", "abstract": "x"}))
        self.assertFalse(is_searchable({"type": "peer-review", "title": "Report on 2108.10326v2", "abstract": "x"}))
        self.assertFalse(is_searchable({"type": "article", "title": "Preface", "abstract": ""}))

    def test_research_texts_stay(self):
        self.assertTrue(is_searchable({"type": "article", "title": "Adaptive Regulation", "abstract": None}))
        self.assertTrue(is_searchable({"type": "dataset", "title": "Sea ice data", "abstract": "Daily ice charts"}))
        self.assertTrue(is_searchable({"type": "article", "title": "Untitled", "abstract": "Production networks"}))


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

    def test_generic_core_words_are_dropped(self):
        plan = QueryPlan(core=["RT-DETR", "ResNet", "архитектуры", "neural architectures", "Architectures"])
        self.assertEqual(plan.core, ["RT-DETR", "ResNet", "neural architectures"])
        self.assertEqual(QueryPlan(core=["методы", "approaches"]).core, ["методы", "approaches"])


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

    def test_profile_lists_coauthors_by_shared_publications(self):
        # A person profile carries every author of the person's papers, each
        # paper weighing 1.0: co-authors rank by how many papers they share.
        profile = {"P1": 1.0, "P2": 1.0}
        persons = rank(_subgraph(), profile, {}, QueryPlan(entities=[{"kind": "person", "name": "Средний"}]),
                       preset("authorship"))["persons"]
        self.assertEqual([(p["id"], p["publications"]) for p in persons], [("middle", 2), ("first", 1)])
        everyone = QueryPlan(entities=[{"kind": "person", "name": "Средний"}], filter={"only_itmo": False})
        persons = rank(_subgraph(), profile, {}, everyone, preset("authorship"))["persons"]
        self.assertIn(("external", 1), [(p["id"], p["publications"]) for p in persons])

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

    def test_llm_failures_are_asked_again(self):
        questions = self.dir / "q.txt"
        questions.write_text("один\n", encoding="utf-8")
        engine = self.FakeEngine()
        ask = engine.ask
        engine.ask = lambda q, o: {**ask(q, o), "trace": {"parse": {"fallback": "ConnectionError"}}}
        options = batch.AskOptions()
        batch.run(questions, self.out, options, engine=engine)
        batch.run(questions, self.out, options, engine=engine)
        self.assertEqual(engine.asked, ["один", "один"])

    def test_review_page_reads_and_saves_marks(self):
        questions = self.dir / "q.txt"
        questions.write_text("один\nдва\n", encoding="utf-8")
        batch.run(questions, self.out, batch.AskOptions(), engine=self.FakeEngine())
        search_dir = self.dir
        self.assertEqual([r["name"] for r in batch.list_runs(search_dir)], ["out"])
        run_dir = batch.resolve_run(search_dir, "out")
        with self.assertRaises(FileNotFoundError):
            batch.resolve_run(search_dir, "../..")
        batch.save_marks(run_dir, "два", {"correct": "нет", "broken_step": "template", "comment": "x"})
        marks = {r["question"]: r["marks"] for r in batch.load_run(run_dir)}
        self.assertEqual(marks["два"], {"correct": "нет", "broken_step": "template", "comment": "x"})
        self.assertEqual(marks["один"]["correct"], "")
        with self.assertRaises(ValueError):
            batch.save_marks(run_dir, "два", {"correct": "может быть"})
        with self.assertRaises(KeyError):
            batch.save_marks(run_dir, "три", {})


class EvaluationTest(unittest.TestCase):
    def test_metrics_of_one_ranking(self):
        grades = {"a": 2, "b": 0, "c": 1, "d": 2}
        m = evaluation.score_ranking(["b", "a", "x", "c"], grades, k=10)
        self.assertEqual((m["P@5"], m["P@10"]), (0.4, 0.2))
        self.assertEqual(m["MRR"], 0.5)  # first relevant at rank 2
        self.assertEqual(m["judged@10"], 0.3)  # x was never judged and counts as not relevant
        self.assertGreater(evaluation.score_ranking(["a", "d", "c"], grades, 10)["nDCG@10"], m["nDCG@10"])
        self.assertEqual(evaluation.score_ranking(["a", "d", "c"], grades, 10)["nDCG@10"], 1.0)

    def test_pool_judgments_and_metrics(self):
        run_dir = Path(tempfile.mkdtemp())
        result = {"question": "q", "plan": {"core": ["x"]}, "sections": {}}
        (run_dir / "results.jsonl").write_text(
            "\n".join(json.dumps(r) for r in [result, {"question": "profile", "plan": {"core": []}}]),
            encoding="utf-8")

        class Engine:
            config = type("C", (), {"search_dir": run_dir})()

            def ask(self, question, options):
                order = ["p1", "p2"] if options.ranking == "graph" else ["p2", "p1"]
                return {"sections": {
                    "persons": [{"id": i, "label": i, "url": None, "top_publications": ["t"]} for i in order],
                    "publications": [{"id": "w1", "label": "Paper", "title": "Paper", "year": 2025}],
                }}

        (run_dir / "publications.json").write_text(json.dumps([{"id": "w1", "text": "Paper about x"}]),
                                                   encoding="utf-8")
        pool = evaluation.build_pool(run_dir, Engine(), k=2)
        entry, skipped = pool["questions"]
        self.assertTrue(skipped["skipped"])
        self.assertEqual(set(entry["items"]["persons"]), {"p1", "p2"})
        self.assertEqual(entry["items"]["publications"]["w1"]["abstract"], "about x")
        evaluation.save_judgment(run_dir, "q", "persons", "p1", 2)
        evaluation.save_judgment(run_dir, "q", "persons", "p2", 0)
        with self.assertRaises(KeyError):
            evaluation.save_judgment(run_dir, "q", "persons", "nobody", 1)
        with self.assertRaises(ValueError):
            evaluation.save_judgment(run_dir, "q", "persons", "p1", 5)
        rows = {r["variant"]: r for r in evaluation.compute_metrics(run_dir)}
        self.assertEqual(rows["hybrid+graph"]["MRR"], 1.0)
        self.assertEqual(rows["hybrid"]["MRR"], 0.5)
        self.assertEqual(evaluation.progress(pool, evaluation.load_judgments(run_dir))["q"],
                         {"judged": 2, "total": 3})
        evaluation.save_judgment(run_dir, "q", "persons", "p2", None)
        self.assertEqual(evaluation.load_judgments(run_dir)["q"], {"persons:p1": 2})


if __name__ == "__main__":
    unittest.main()
