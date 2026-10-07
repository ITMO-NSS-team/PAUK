from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pauk.rag import evaluation, index
from pauk.rag.graph import escape_lucene
from pauk.rag.profiles import PRESETS, Profile, resolve
from pauk.rag.readme import clean_readme
from pauk.rag.retrieve import Hit, Rag, fuse, min_max


class _Config:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.rag_embedding_model = "fake-model"


class _Embedder:
    dimensions = 3

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, progress=False):
        self.calls.append(list(texts))
        return np.array([[1.0, 0.0, 0.0]] * len(texts), dtype=np.float32)


class _IndexGraph:
    """Records writes; serves nodes as Neo4j would, including stored hashes."""

    def __init__(self, nodes: dict[str, list[dict]]) -> None:
        self.nodes_by_label = nodes
        self.stored: dict[str, list[dict]] = {}
        self.cleared: dict[str, list[str]] = {}
        self.indexes: list[tuple[str, str]] = []

    def nodes(self, label, profile):
        return [dict(row) for row in self.nodes_by_label.get(label, [])]

    def store(self, label, profile, rows):
        self.stored.setdefault(label, []).extend(rows)

    def clear(self, label, profile, ids):
        self.cleared.setdefault(label, []).extend(ids)

    def ensure_indexes(self, label, profile, dimensions, analyzer):
        self.indexes.append((profile.vector_index(label), profile.text_index(label)))

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
        self.assertEqual(profile.vector_index("Repository"), "rag_vec_no_readme_repository")
        self.assertEqual(profile.text_property, "rag_text_no_readme")

    def test_short_readme_contributes_nothing(self):
        row = {"name": "osgud", "readme": "# Osgud\n![badge](http://x/y.svg)"}
        self.assertEqual(PRESETS["full"].compose("Repository", row), "osgud")


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
        unchanged = index.text_hash("fake-model", "Kept. Same")
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
        self.assertTrue(index.manifest_path(self.config, "p").is_file())
        self.assertEqual(graph.indexes, [("rag_vec_p_publication", "rag_text_p_publication")])

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
    def test_translation_is_on_by_default_and_degrades_with_a_note(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _Config(Path(tmp.name))
        manifest = index.manifest_path(config, "full")
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"profile": PRESETS["full"].to_dict(), "embedding_model": "fake-model"}))
        graph = _AskGraph()
        rag = Rag(config, graph=graph, embedder=_Embedder(), llm=_FailingLlm())
        result = rag.ask("Кто занимается RT-DETR?", with_answer=False)
        self.assertEqual(len(result["notes"]), 1)
        self.assertFalse(result["options"]["translate"])
        self.assertEqual(graph.terms, r"Кто занимается RT\-DETR\?")

    def test_translation_reaches_bm25_with_one_llm_call(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _Config(Path(tmp.name))
        manifest = index.manifest_path(config, "full")
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"profile": PRESETS["full"].to_dict(), "embedding_model": "fake-model"}))
        graph = _AskGraph()
        llm = _CountingLlm()
        rag = Rag(config, graph=graph, embedder=_Embedder(), llm=llm)
        result = rag.ask("Кто занимается RT-DETR?", with_answer=False)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result["notes"], [])
        self.assertEqual(graph.terms, r"RT\-DETR object detection")


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
                         {"profile": "full", "mode": "hybrid", "fusion": "rrf", "translate": True})
        self.assertFalse(evaluation.parse_variant("full:bm25")["translate"])
        for bad in ("full:sparse", "full:bm25:ru"):
            with self.assertRaises(ValueError):
                evaluation.parse_variant(bad)


if __name__ == "__main__":
    unittest.main()
