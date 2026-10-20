from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pauk.rag import evaluation, index
from pauk.rag.embedder import Embedder
from pauk.rag.graph import escape_lucene
from pauk.rag.models import MODELS
from pauk.rag.profiles import PRESETS, Profile, resolve
from pauk.rag.readme import clean_readme
from pauk.rag.retrieve import Hit, Rag, fuse, min_max


class _Config:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.rag_embedding_model = "BAAI/bge-m3"


class _Embedder:
    dimensions = 3

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, progress=False, query=False):
        self.calls.append(list(texts))
        return np.array([[1.0, 0.0, 0.0]] * len(texts), dtype=np.float32)


class _IndexGraph:
    """Records writes; serves nodes as Neo4j would, including stored hashes."""

    def __init__(self, nodes: dict[str, list[dict]]) -> None:
        self.nodes_by_label = nodes
        self.stored: dict[str, list[dict]] = {}
        self.cleared: dict[str, list[str]] = {}
        self.indexes: list[tuple[str, str]] = []

    def nodes(self, label, profile, model):
        return [dict(row) for row in self.nodes_by_label.get(label, [])]

    def store(self, label, profile, model, rows):
        self.stored.setdefault(label, []).extend(rows)

    def clear(self, label, profile, ids):
        self.cleared.setdefault(label, []).extend(ids)

    def ensure_indexes(self, label, profile, model, dimensions, analyzer):
        self.indexes.append((profile.vector_index(label, model), profile.text_index(label)))

    def await_indexes(self):
        pass


class ProfileTest(unittest.TestCase):
    def test_compose_joins_fields_in_order_and_skips_empty_ones(self):
        profile = Profile("p", ("title", "abstract", "fields"), ())
        text = profile.compose("Publication", {"title": "Graph nets.", "abstract": "", "fields": ["CS", "Math"]})
        self.assertEqual(text, "Graph nets. CS, Math")

    def test_names_and_fields_are_validated(self):
        with self.assertRaises(ValueError):
            Profile("Bad-Name", ("title",))
        with self.assertRaises(ValueError):
            Profile("p", ("title", "topics"))
        with self.assertRaises(ValueError):
            Profile("p")

    def test_presets_differ_from_full_in_one_place(self):
        full = PRESETS["full"]
        self.assertNotIn("readme", PRESETS["no_readme"].repository)
        self.assertEqual(PRESETS["no_readme"].publication, full.publication)
        self.assertEqual(resolve("custom", None, ["name"]).repository, ("name",))

    def test_neo4j_names_carry_the_profile(self):
        profile = PRESETS["no_readme"]
        self.assertEqual(profile.vector_index("Repository", "qwen3"), "rag_vec_no_readme_qwen3_repository")
        self.assertEqual(profile.vector_property("bge"), "rag_vec_no_readme_bge")
        # The text and its BM25 index do not depend on the model.
        self.assertEqual(profile.text_property, "rag_text_no_readme")
        self.assertEqual(profile.text_index("Repository"), "rag_text_no_readme_repository")

    def test_short_readme_contributes_nothing(self):
        row = {"name": "osgud", "readme": "# Osgud\n![badge](http://x/y.svg)"}
        self.assertEqual(PRESETS["full"].compose("Repository", row), "osgud")


class _SentenceModel:
    def __init__(self):
        self.texts: list[str] = []

    def encode(self, texts, **kwargs):
        self.texts.extend(texts)
        return np.ones((len(texts), 3), dtype=np.float32)


class EmbedderTest(unittest.TestCase):
    def test_instruction_goes_before_the_question_only(self):
        embedder = Embedder(MODELS["qwen3"])
        embedder._model = model = _SentenceModel()
        embedder.encode(["Кто занимается льдом?"], query=True)
        embedder.encode(["Sea ice. Forecasting"])
        self.assertTrue(model.texts[0].startswith("Instruct: "))
        self.assertTrue(model.texts[0].endswith("Query:Кто занимается льдом?"))
        self.assertEqual(model.texts[1], "Sea ice. Forecasting")


class ReadmeCleaningTest(unittest.TestCase):
    def test_keeps_prose_and_drops_code_badges_and_links(self):
        text = """# Tool
[![Build](https://ci/x.svg)](https://ci/x)
A library for **federated learning** on [edge devices](https://example.com).

```bash
pip install tool
```
    indented_code()
$ tool --run
<img src="logo.png"> See https://docs.example.com
"""
        cleaned = clean_readme(text)
        self.assertIn("A library for federated learning on edge devices", cleaned)
        for noise in ("pip install", "indented_code", "--run", "https://", "Build", "<img"):
            self.assertNotIn(noise, cleaned)


class FusionTest(unittest.TestCase):
    def test_min_max_and_weighted_fusion(self):
        self.assertEqual(min_max([("a", 3.0), ("b", 1.0)]), {"a": 1.0, "b": 0.0})
        scores = fuse([("a", 0.9), ("b", 0.5)], [("b", 20.0), ("c", 5.0)], "minmax")
        self.assertAlmostEqual(scores["a"], 0.7)
        self.assertAlmostEqual(scores["b"], 0.3)
        self.assertEqual(scores["c"], 0.0)

    def test_rrf_rewards_items_both_sides_found(self):
        scores = fuse([("a", 0.9), ("b", 0.5)], [("b", 20.0), ("c", 5.0)], "rrf")
        self.assertGreater(scores["b"], scores["a"])

    def test_lucene_operators_are_escaped(self):
        self.assertEqual(escape_lucene("RT-DETR (v2)?"), r"RT\-DETR \(v2\)\?")


class IndexBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _Config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_embeds_changed_texts_only_and_clears_dropped_nodes(self):
        profile = Profile("p", ("title", "abstract"), ())
        unchanged = index.text_hash("BAAI/bge-m3", "Kept. Same")
        graph = _IndexGraph({"Publication": [
            {"id": "W1", "title": "Kept", "abstract": "Same", "type": "article", "hash": unchanged},
            {"id": "W2", "title": "New", "abstract": "Text", "type": "article", "hash": None},
            {"id": "W3", "title": "Contributors", "abstract": None, "type": "paratext", "hash": "old"},
            {"id": "W4", "title": "Figure 2", "abstract": None, "type": "other", "hash": None},
        ]})
        embedder = _Embedder()
        manifest = index.build(self.config, profile, embedder=embedder, graph=graph)
        self.assertEqual(embedder.calls, [["New. Text"]])
        self.assertEqual([row["id"] for row in graph.stored["Publication"]], ["W2"])
        self.assertEqual(graph.cleared["Publication"], ["W3"])  # was indexed before, now filtered out
        self.assertEqual(manifest["labels"]["Publication"]["indexed"], 2)
        self.assertEqual(manifest["labels"]["Publication"]["not_research"], 2)
        self.assertTrue(index.manifest_path(self.config, "p", "bge").is_file())
        self.assertEqual(graph.indexes, [("rag_vec_p_bge_publication", "rag_text_p_publication")])

    def test_a_second_profile_reuses_vectors_of_identical_texts(self):
        rows = {"Publication": [{"id": "W1", "title": "Same", "abstract": "Text", "type": "article", "hash": None}]}
        index.build(self.config, Profile("first", ("title", "abstract"), ()), embedder=_Embedder(),
                    graph=_IndexGraph(rows))
        embedder, graph = _Embedder(), _IndexGraph(rows)
        manifest = index.build(self.config, Profile("second", ("title", "abstract"), ()), embedder=embedder,
                               graph=graph)
        self.assertEqual(embedder.calls, [])  # written to the new profile, but not embedded again
        self.assertEqual(manifest["labels"]["Publication"]["written"], 1)
        self.assertEqual(graph.stored["Publication"][0]["vector"], [1.0, 0.0, 0.0])

    def test_readme_comes_from_the_cache(self):
        readmes = Path(self.tmp.name) / "rag" / "readmes"
        readmes.mkdir(parents=True)
        (readmes / "R1.md").write_text("A toolkit for sea ice forecasting with neural networks trained on "
                                       "satellite observations of the Arctic, with evaluation scripts, data loaders "
                                       "and pretrained weights for the Barents and Kara seas.")
        graph = _IndexGraph({"Repository": [{"id": "R1", "name": "ice", "description": None, "implemented": []}]})
        index.build(self.config, Profile("p", (), ("name", "readme")), embedder=_Embedder(), graph=graph)
        self.assertIn("sea ice forecasting", graph.stored["Repository"][0]["text"])


class _ExpertsGraph:
    def read(self, query, **params):
        if "AUTHORED]->(pub" in query:
            return [{"item": "W1", "person": "a"}, {"item": "W1", "person": "b"},
                    {"item": "W2", "person": "b"}, {"item": "W2", "person": "ext"}]
        if "CONTRIBUTED_TO]->(repo" in query:
            return [{"item": i, "person": "c"} for i in params["ids"] if i == "R1"]
        if "MATCH (p:Person" in query:
            names = {"a": "А", "b": "Б", "c": "В", "ext": "External"}
            return [{"id": i, "name_ru": names[i], "is_itmo": i != "ext", "departments": [], "orcid": None,
                     "openalex_id": None} for i in params["ids"]]
        raise AssertionError(query)


class ExpertsTest(unittest.TestCase):
    def test_people_ranked_by_squared_relevance_of_their_items(self):
        rag = Rag(_Config(Path(".")), graph=_ExpertsGraph(), embedder=_Embedder())
        hits = {"Publication": [Hit("W1", "Publication", 0.9), Hit("W2", "Publication", 0.5),
                                Hit("W3", "Publication", 0.1)],
                "Repository": [Hit("R1", "Repository", 0.8)]}
        people = rag.experts(hits)
        # W1 -> 1.0, W2 -> 0.5 after min-max over 0.9..0.1; squared: 1.0 and 0.25.
        self.assertEqual([p["id"] for p in people], ["b", "a", "c"])
        self.assertAlmostEqual(people[0]["score"], 1.25)
        self.assertAlmostEqual(people[2]["score"], 0.5)  # one repository at half the weight of a paper
        self.assertEqual(people[0]["publications"], ["W1", "W2"])
        self.assertNotIn("ext", [p["id"] for p in rag.experts(hits)])
        self.assertIn("ext", [p["id"] for p in rag.experts(hits, only_itmo=False)])


class _FailingLlm:
    model = "m"
    last_error = "no key"

    def chat_json(self, *args, **kwargs):
        return None


class _CountingLlm:
    model = "m"
    last_error = None

    def __init__(self):
        self.calls = 0

    def chat_json(self, *args, **kwargs):
        self.calls += 1
        return {"query": "RT-DETR object detection"}


class _AskGraph(_ExpertsGraph):
    terms = None

    def vector_search(self, label, index, vector, k):
        return [("W1", 0.9), ("W2", 0.5)] if label == "Publication" else []

    def text_search(self, index, terms, k):
        self.terms = terms
        return []

    def read(self, query, **params):
        if "RETURN p.id AS id, p.title AS title" in query or "RETURN r.id AS id, r.name AS name" in query:
            return []  # details for display; not under test here
        return super().read(query, **params)


class AskTest(unittest.TestCase):
    def test_keywords_are_on_by_default_and_degrade_with_a_note(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _Config(Path(tmp.name))
        manifest = index.manifest_path(config, "full", "bge")
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"profile": PRESETS["full"].to_dict(), "embedding_model": "BAAI/bge-m3"}))
        graph = _AskGraph()
        rag = Rag(config, graph=graph, embedder=_Embedder(), llm=_FailingLlm())
        result = rag.ask("Кто занимается RT-DETR?", with_answer=False)
        self.assertEqual(len(result["notes"]), 1)
        self.assertFalse(result["options"]["keywords"])
        self.assertEqual(graph.terms, r"Кто занимается RT\-DETR\?")

    def test_a_model_without_its_own_build_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _Config(Path(tmp.name))
        manifest = index.manifest_path(config, "full", "bge")
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"profile": PRESETS["full"].to_dict(), "embedding_model": "BAAI/bge-m3"}))
        rag = Rag(config, graph=_AskGraph(), embedder=_Embedder(), llm=_FailingLlm())
        self.assertEqual(rag.profile("full").name, "full")
        with self.assertRaises(ValueError):
            rag.profile("full", "qwen3")  # its vectors are not in Neo4j: never query bge vectors with qwen3

    def test_keywords_reach_bm25_with_one_llm_call(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _Config(Path(tmp.name))
        manifest = index.manifest_path(config, "full", "bge")
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"profile": PRESETS["full"].to_dict(), "embedding_model": "BAAI/bge-m3"}))
        graph = _AskGraph()
        llm = _CountingLlm()
        rag = Rag(config, graph=graph, embedder=_Embedder(), llm=llm)
        result = rag.ask("Кто занимается RT-DETR?", with_answer=False)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result["notes"], [])
        self.assertEqual(graph.terms, r"RT\-DETR object detection")

    def test_given_keywords_replace_the_extraction(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _Config(Path(tmp.name))
        manifest = index.manifest_path(config, "full", "bge")
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"profile": PRESETS["full"].to_dict(), "embedding_model": "BAAI/bge-m3"}))
        graph, embedder, llm = _AskGraph(), _Embedder(), _CountingLlm()
        rag = Rag(config, graph=graph, embedder=embedder, llm=llm)
        result = rag.ask("Кто занимается RT-DETR?", query_keywords=" RT-DETR detector ", with_answer=False)
        self.assertEqual(llm.calls, 0)  # the planner already paid for them
        self.assertEqual(graph.terms, r"RT\-DETR detector")
        self.assertEqual(embedder.calls, [["RT-DETR detector"]])
        self.assertEqual(result["options"]["keywords_source"], "given")
        rag.ask("Кто занимается RT-DETR?", query_keywords="  ", with_answer=False)
        self.assertEqual(llm.calls, 1)  # blank keywords are none: extract as usual

    def test_dense_keywords_replace_the_question_for_the_encoder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _Config(Path(tmp.name))
        manifest = index.manifest_path(config, "full", "bge")
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"profile": PRESETS["full"].to_dict(), "embedding_model": "BAAI/bge-m3"}))
        embedder, llm = _Embedder(), _CountingLlm()
        rag = Rag(config, graph=_AskGraph(), embedder=embedder, llm=llm)
        rag.ask("Кто занимается RT-DETR?", with_answer=False)  # on by default
        self.assertEqual(embedder.calls, [["RT-DETR object detection"]])
        self.assertEqual(llm.calls, 1)  # one extraction serves both sides


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _pool(self):
        entries = []
        for n, (good, bad) in enumerate([("x", "y"), ("x", "y"), ("x", "y")]):
            entries.append({
                "question": f"q{n}", "meta": {},
                "rankings": {"full:hybrid": {"persons": [good, bad], "publications": [], "repositories": []},
                             "no_readme:hybrid": {"persons": [bad, good], "publications": [], "repositories": []}},
                "items": {"persons": {good: {}, bad: {}}, "publications": {}, "repositories": {}},
                "order": {"persons": [good, bad], "publications": [], "repositories": []},
            })
        pool = {"k": 10, "variants": {}, "kinds": list(evaluation.KINDS), "questions": entries}
        (self.run / "pool.json").write_text(json.dumps(pool))

    def test_ndcg_and_sign_test_against_baseline(self):
        self._pool()
        for n in range(3):
            evaluation.save_judgment(self.run, f"q{n}", "persons", "x", 2)
            evaluation.save_judgment(self.run, f"q{n}", "persons", "y", 0)
        rows = {row["variant"]: row for row in evaluation.compute_metrics(self.run, baseline="full:hybrid")}
        self.assertEqual(rows["full:hybrid"]["nDCG@10"], 1.0)
        self.assertLess(rows["no_readme:hybrid"]["nDCG@10"], 1.0)
        self.assertEqual((rows["no_readme:hybrid"]["wins"], rows["no_readme:hybrid"]["losses"]), (0, 3))
        self.assertEqual(rows["no_readme:hybrid"]["p"], 0.25)  # 3 of 3: two-sided 2 * (1/2)^3
        # No relevant repository judged in any question: that kind is left out, not scored 0.
        self.assertEqual({row["kind"] for row in rows.values()}, {"persons"})

    def test_cutoffs_are_chosen_and_recall_is_over_judged_relevant_items(self):
        grades = {"a": 2, "b": 0, "c": 1, "d": 1}  # three relevant; "d" never retrieved
        scores = evaluation.score_ranking(["b", "a", "c"], grades, (1, 3))
        self.assertEqual((scores["P@1"], scores["R@1"]), (0.0, 0.0))
        self.assertAlmostEqual(scores["P@3"], 2 / 3)
        self.assertAlmostEqual(scores["R@3"], 2 / 3)
        self.assertEqual(scores["MRR"], 0.5)
        self.assertIn("nDCG@3", scores)
        self._pool()
        evaluation.save_judgment(self.run, "q0", "persons", "x", 2)
        rows = evaluation.compute_metrics(self.run, cutoffs=(1, 2))
        self.assertIn("R@2", rows[0])
        self.assertNotIn("P@10", rows[0])
        with self.assertRaises(ValueError):  # the pool holds each variant's top 10 only
            evaluation.compute_metrics(self.run, cutoffs=(20,))

    def test_calibration_takes_the_quantile_of_off_topic_matches(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        questions = Path(tmp.name) / "offtopic.csv"
        questions.write_text("question,type,targets\n" + "".join(f"q{i},offtopic,persons\n" for i in range(5)),
                             encoding="utf-8")

        class _Rag:
            scores = iter([0.70, 0.72, 0.74, 0.76, 0.90])

            def retrieve(self, question, **kwargs):
                return {"Publication": [Hit("W", "Publication", 1.0, dense=next(self.scores))]}

        result = evaluation.calibrate(_Rag(), questions, "full@qwen3:dense", quantile=0.75)
        self.assertEqual(result["min_score"], 0.76)
        self.assertEqual([s["question"] for s in result["above"]], ["q4", "q3"])

    def test_auc_and_best_threshold(self):
        self.assertEqual(evaluation._auc([0.9, 0.8], [0.5, 0.6]), 1.0)
        self.assertEqual(evaluation._auc([0.5], [0.5]), 0.5)
        cut = evaluation._best_threshold([0.9, 0.8, 0.4], [0.5, 0.3])
        self.assertEqual((cut["threshold"], cut["answered"], cut["false_answers"]), (0.8, 0.667, 0.0))

    def test_empty_answers_are_counted_against_the_model_threshold(self):
        def entry(kind, top):
            return {"meta": {"type": kind, "targets": "persons,publications"},
                    "rankings": {"full@qwen3:dense": {"top_dense": {"Publication": top}}}}
        pool = {"variants": {"full@qwen3:dense": evaluation.parse_variant("full@qwen3:dense")},
                "questions": [entry("no_answer", 0.70), entry("no_answer", 0.80), entry("topic", 0.90),
                              entry("topic", 0.70)]}
        row = evaluation.abstention(pool)[0]
        self.assertEqual((row["empty_right"], row["unanswerable"]), (1, 2))
        self.assertEqual((row["empty_wrong"], row["answerable"]), (1, 2))

    def test_grades_are_checked_and_seeded_without_overwriting(self):
        self._pool()
        with self.assertRaises(KeyError):
            evaluation.save_judgment(self.run, "q0", "persons", "nobody", 1)
        with self.assertRaises(ValueError):
            evaluation.save_judgment(self.run, "q0", "persons", "x", 5)
        evaluation.save_judgment(self.run, "q0", "persons", "x", 1)
        source = self.run / "earlier.json"
        source.write_text(json.dumps({"q0": {"persons:x": 2, "persons:y": 0, "persons:gone": 1}}))
        self.assertEqual(evaluation.seed_judgments(self.run, source), 1)
        self.assertEqual(evaluation.load_judgments(self.run)["q0"], {"persons:x": 1, "persons:y": 0})

    def test_kappa(self):
        self.assertEqual(evaluation.kappa([(0, 0), (1, 1), (2, 2)], weighted=False), 1.0)
        self.assertLess(evaluation.kappa([(0, 2), (2, 0), (1, 1)], weighted=True), 0.5)

    def test_agreement_has_a_question_bootstrap_interval(self):
        self._pool()
        for n in range(3):
            evaluation.save_judgment(self.run, f"q{n}", "persons", "x", 2, judge="a")
            evaluation.save_judgment(self.run, f"q{n}", "persons", "x", 2, judge="b")
            evaluation.save_judgment(self.run, f"q{n}", "persons", "y", n % 2, judge="a")
            evaluation.save_judgment(self.run, f"q{n}", "persons", "y", 0, judge="b")
        rows = {r["kind"]: r for r in evaluation.agreement(self.run, "b", "a", resamples=50)}
        self.assertEqual((rows["all"]["items"], rows["all"]["questions"]), (6, 3))
        self.assertEqual(len(rows["all"]["weighted_kappa_ci"]), 2)

    def test_person_context_finds_works_by_person_and_title(self):
        class _Graph:
            def read(self, query, rows):
                self.rows = rows
                return [{"person": "a", "id": "W1", "title": "GNN", "year": 2024, "abstract": "graphs " * 200}]

        pool = {"questions": [{"items": {"persons": {"a": {"evidence": ["GNN", "Lost title"]},
                                                     "b": {"evidence": []}}}}]}
        graph = _Graph()
        self.assertEqual(evaluation.add_person_context(graph, pool), 1)
        entry = pool["questions"][0]
        self.assertEqual(graph.rows, [{"person": "a", "titles": ["GNN", "Lost title"]}])
        self.assertEqual(entry["items"]["persons"]["a"]["evidence_ids"], ["W1"])
        self.assertEqual(entry["items"]["persons"]["b"]["evidence_ids"], [])
        self.assertLessEqual(len(entry["context"]["publications"]["W1"]["abstract"]), evaluation.SNIPPET_CHARS + 1)

    def test_judge_pool_grades_one_item_per_request_and_logs_it(self):
        entry = {"question": "q", "meta": {"expected": "rule"},
                 "rankings": {"v": {"publications": ["a", "b", "c"], "repositories": [], "persons": ["p"]}},
                 "items": {"persons": {"p": {}}, "repositories": {},
                           "publications": {i: {"title": i, "abstract": "", "full_text": ""} for i in "abc"}},
                 "order": {"persons": ["p"], "publications": ["a", "b", "c"], "repositories": []}}
        (self.run / "pool.json").write_text(json.dumps({"k": 3, "variants": {"v": {}}, "kinds": list(evaluation.KINDS),
                                                        "questions": [entry]}))

        class _Llm:
            model = "m"
            last_error = None
            last_usage = {"prompt_tokens": 10, "completion_tokens": 1}

            def chat_json(self, prompt, system_prompt=None, max_tokens=None, reasoning=None):
                item = json.loads(prompt)["элемент"]["название"]
                return {"grade": {"a": 2, "b": 0}.get(item, "high")}  # "c" gets no valid grade

        result = evaluation.judge_pool(self.run, _Llm, "q1", workers=2)
        self.assertEqual((result["graded"], result["failed"], result["prompt_tokens"]), (2, 1, 30))
        self.assertEqual(evaluation.load_judgments(self.run, "q1")["q"], {"publications:a": 2, "publications:b": 0})
        log = [json.loads(line) for line in (self.run / "judge_q1.log.jsonl").read_text().splitlines()]
        self.assertEqual(len(log), 3)  # every request, failed ones included; people are never asked
        config = json.loads((self.run / "judge_q1.config.json").read_text())
        self.assertEqual(config["prompt_id"], hashlib.sha1(evaluation.judge_system(3).encode()).hexdigest()[:8])
        rows = evaluation.tails(evaluation.load_pool(self.run), evaluation.load_judgments(self.run, "q1"))
        self.assertEqual([(r["last_relevant"], r["unjudged"]) for r in rows], [(1, 1)])
        self.assertIn("первые 3 элементов", config["system_prompt"])  # the pool depth is stated

        class _Other(_Llm):
            model = "other"

        with self.assertRaises(ValueError):  # one judgments file, one judge
            evaluation.judge_pool(self.run, _Other, "q1")

    def test_person_grade_is_the_best_grade_of_their_works(self):
        pool = {"questions": [{"question": "q", "items": {"persons": {
            "a": {"works": {"publications": ["w1", "w2"], "repositories": ["r1"]}},
            "b": {"works": {"publications": ["w2"], "repositories": []}},
            "c": {"works": {"publications": ["lost"], "repositories": []}}}}}]}
        judgments = {"q": {"publications:w1": 1, "publications:w2": 0, "repositories:r1": 2}}
        counts = evaluation.person_grades(pool, judgments)
        self.assertEqual((judgments["q"]["persons:a"], judgments["q"]["persons:b"]), (2, 0))
        self.assertNotIn("persons:c", judgments["q"])  # resting on an ungraded work only: no grade
        self.assertEqual((counts["no_grade"], counts["ungraded_works"]), (1, 1))

    def test_questions_csv_keeps_extra_columns(self):
        path = self.run / "q.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerows([["question", "type", "targets"], ["Кто?", "topic", "repositories"], ["Кто?", "", ""]])
        self.assertEqual(evaluation.read_questions(path), [{"question": "Кто?", "type": "topic",
                                                             "targets": "repositories"}])

    def test_targets_limit_the_judged_kinds(self):
        self.assertEqual(evaluation.targets({"targets": "repositories"}), ("repositories",))
        self.assertEqual(evaluation.targets({"targets": "publications, persons"}), ("persons", "publications"))
        self.assertEqual(evaluation.targets({}), evaluation.KINDS)
        with self.assertRaises(ValueError):
            evaluation.targets({"targets": "people"})

    def test_variant_syntax(self):
        self.assertEqual(evaluation.parse_variant("full:hybrid:rrf+en"),
                         {"profile": "full", "model": "bge", "mode": "hybrid", "fusion": "rrf", "bm25_keywords": True,
                          "dense_keywords": False})
        self.assertTrue(evaluation.parse_variant("full:dense:kw")["dense_keywords"])
        self.assertFalse(evaluation.parse_variant("full:bm25")["bm25_keywords"])
        self.assertEqual(evaluation.parse_variant("full@qwen3:dense")["model"], "qwen3")
        for bad in ("full:sparse", "full:bm25:ru"):
            with self.assertRaises(ValueError):
                evaluation.parse_variant(bad)


if __name__ == "__main__":
    unittest.main()
