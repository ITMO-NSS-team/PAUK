import json
import tempfile
import unittest
from pathlib import Path

from pauk.gui.generate_paper_graphs import build_paper_graphs, write_paper_graphs
from pauk.paper_graph import file_url, load_export, load_paper_graphs, parse_markdown_sections

DOCUMENT_MD = """# Deep Nets for Graphs

Intro text.

## 2 Method

We use a two-tower encoder.

```python
# 2.1 not a heading, it is a comment
```

## 3 Experiments

Results follow.

## 4 Conclusion

Nothing was extracted from this one.
"""

CLAIMS_LEGACY = {
    "result": [
        {
            "claim_id": "c1",
            "claim": "The encoder has two towers.",
            "original_text": "We use a two-tower encoder.",
            "category": "model_architecture",
            "verifiability": "high",
            "section_name": "Method",
            "section_heading_raw": "## 2 Method",
        },
        {
            "claim_id": "c2",
            "claim": "F1 reaches 0.91 on the benchmark.",
            "original_text": "Results follow.",
            "category": "numerical_result",
            "value": "0.91",
            "verifiability": "medium",
            "section_name": "Experiments",
            "contradiction": True,
        },
    ],
    "meta": {"model": "test"},
}

EXPERIMENTS = [
    {
        "description_from_paper": "The encoder has two towers.",
        "impl_src_path": ["/tmp/clone/repo/src/models/encoder.py:42", "train.py"],
        "missing": ["weight tying"],
        "correspondence_percent": 80.0,
        "reasoning": "Both towers are built in encoder.py.",
    },
    {
        "description_from_paper": "Training runs for 100 epochs.",
        "impl_src_path": ["train.py"],
        "correspondence_percent": 100.0,
    },
]


LAYOUT = {
    "elements": [
        {"bbox_idx": 4, "node_id": "elem_4", "page_num": 1, "name": "plain text",
         "text": "We use a two-tower  encoder.\nIt is trained end to end."},
        {"bbox_idx": 9, "node_id": "elem_9", "page_num": 2, "name": "figure", "text": ""},
        {"bbox_idx": 10, "node_id": "elem_10", "page_num": 2, "name": "figure caption",
         "text": "Figure 1: the two towers."},
    ]
}

DOC_GRAPH = {
    "nodes": [
        {"node_id": "elem_4", "node_type": "element", "name": "plain text", "page_num": 1},
        {"node_id": "elem_9", "node_type": "element", "name": "figure", "page_num": 2},
        {"node_id": "elem_10", "node_type": "element", "name": "figure caption", "page_num": 2},
        {"node_id": "elem_11", "node_type": "element", "name": "plain text", "page_num": 2,
         "text": "Results follow."},
        {"node_id": "elem_12", "node_type": "element", "name": "plain text", "page_num": 3,
         "text": "Far away."},
        {"node_id": "page_1", "node_type": "page", "page_num": 1},
    ],
    "edges": [
        {"source": "elem_4", "target": "elem_9", "edge_type": "text_references",
         "matched_pattern": "Figure 1", "reference_category": "figure"},
        {"source": "elem_10", "target": "elem_9", "edge_type": "caption_of"},
        {"source": "elem_10", "target": "elem_11", "edge_type": "reading_order"},
        {"source": "elem_11", "target": "elem_12", "edge_type": "reading_order"},
        {"source": "page_1", "target": "elem_4", "edge_type": "page_contains"},
    ],
}


def _write_layout(directory: Path) -> Path:
    (directory / "layout.json").write_text(json.dumps(LAYOUT), encoding="utf-8")
    (directory / "graph.json").write_text(json.dumps(DOC_GRAPH), encoding="utf-8")
    return directory


def _write_export(root: Path, pub_id: str = "W1") -> Path:
    directory = root / pub_id
    directory.mkdir(parents=True)
    (directory / "document.md").write_text(DOCUMENT_MD, encoding="utf-8")
    (directory / "claims_legacy.json").write_text(json.dumps(CLAIMS_LEGACY), encoding="utf-8")
    (directory / "experiments.json").write_text(json.dumps(EXPERIMENTS), encoding="utf-8")
    (directory / "meta.json").write_text(
        json.dumps({"title": "Deep Nets", "repository_url": "https://github.com/org/repo"}),
        encoding="utf-8",
    )
    return directory


class MarkdownSectionsTest(unittest.TestCase):
    def test_headings_become_ordered_sections(self):
        sections = parse_markdown_sections(DOCUMENT_MD)
        self.assertEqual(
            [(s.section_id, s.numbering, s.name, s.level) for s in sections],
            [
                ("s001", None, "Deep Nets for Graphs", 1),
                ("s002", "2", "Method", 2),
                ("s003", "3", "Experiments", 2),
                ("s004", "4", "Conclusion", 2),
            ],
        )

    def test_hash_inside_a_code_fence_is_not_a_heading(self):
        names = [s.name for s in parse_markdown_sections(DOCUMENT_MD)]
        self.assertNotIn("not a heading, it is a comment", names)


class FileUrlTest(unittest.TestCase):
    def test_clone_prefix_and_line_number_are_normalized(self):
        self.assertEqual(
            file_url("https://github.com/org/repo", "/tmp/clone/repo/src/models/encoder.py:42"),
            "https://github.com/org/repo/blob/HEAD/src/models/encoder.py#L42",
        )

    def test_no_url_without_a_github_repository(self):
        self.assertIsNone(file_url(None, "train.py"))
        self.assertIsNone(file_url("https://gitlab.com/org/repo", "train.py"))


class LoadExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_claims_join_their_section_by_name(self):
        graph = load_export(_write_export(self.root))
        by_id = {c.claim_id: c for c in graph.claims}
        sections = {s.section_id: s.name for s in graph.sections}
        self.assertEqual(sections[by_id["c1"].section_id], "Method")
        self.assertEqual(sections[by_id["c2"].section_id], "Experiments")

    def test_sections_nothing_was_extracted_from_are_dropped(self):
        graph = load_export(_write_export(self.root))
        self.assertEqual(sorted(s.name for s in graph.sections), ["Experiments", "Method"])

    def test_experiment_files_land_on_the_matching_claim(self):
        graph = load_export(_write_export(self.root))
        claim = next(c for c in graph.claims if c.claim_id == "c1")
        self.assertEqual([f.path for f in claim.files],
                         ["/tmp/clone/repo/src/models/encoder.py:42", "train.py"])
        self.assertEqual(claim.files[0].url,
                         "https://github.com/org/repo/blob/HEAD/src/models/encoder.py#L42")
        self.assertEqual(claim.correspondence_percent, 80.0)
        self.assertEqual(claim.missing, ["weight tying"])

    def test_unmatched_experiment_becomes_its_own_claim(self):
        graph = load_export(_write_export(self.root))
        extra = [c for c in graph.claims if c.claim == "Training runs for 100 epochs."]
        self.assertEqual(len(extra), 1)
        self.assertIsNone(extra[0].section_id)
        self.assertEqual([f.path for f in extra[0].files], ["train.py"])

    def test_publication_id_comes_from_the_directory_name(self):
        graph = load_export(_write_export(self.root, "W7164652155"))
        self.assertEqual(graph.publication_id, "W7164652155")
        self.assertEqual(graph.title, "Deep Nets")

    def test_a_directory_without_claims_is_skipped(self):
        empty = self.root / "W2"
        empty.mkdir()
        (empty / "document.md").write_text(DOCUMENT_MD, encoding="utf-8")
        self.assertIsNone(load_export(empty))

    def test_sections_json_wins_over_the_markdown(self):
        directory = _write_export(self.root)
        (directory / "sections.json").write_text(
            json.dumps([
                {"name": "Method", "text": "", "heading_meta": {"raw": "## 2 Method",
                                                                "level": 2, "numbering": "2"}},
                {"name": "Experiments", "text": "", "heading_meta": {"raw": "## 3 Experiments",
                                                                    "level": 2, "numbering": "3"}},
            ]),
            encoding="utf-8",
        )
        graph = load_export(directory)
        self.assertEqual([s.section_id for s in graph.sections], ["s001", "s002"])
        self.assertEqual([s.name for s in graph.sections], ["Method", "Experiments"])


class ClaimSourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _claims(self, with_layout: bool):
        directory = _write_export(self.root)
        if with_layout:
            _write_layout(directory)
        return {c.claim_id: c for c in load_export(directory).claims}

    def test_the_sentence_itself_is_a_source_without_any_layout(self):
        claim = self._claims(with_layout=False)["c1"]
        self.assertEqual([s.kind for s in claim.sources], ["quote"])
        self.assertEqual(claim.sources[0].text, "We use a two-tower encoder.")
        self.assertEqual(claim.sources[0].label, "Method")
        self.assertIsNone(claim.sources[0].page)

    def test_a_claim_with_no_evidence_gets_no_sources(self):
        claim = next(c for c in load_export(_write_export(self.root)).claims
                     if c.claim == "Training runs for 100 epochs.")
        self.assertEqual(claim.sources, [])

    def test_the_page_element_is_found_across_reflowed_whitespace(self):
        claim = self._claims(with_layout=True)["c1"]
        element = next(s for s in claim.sources if s.kind == "element")
        self.assertEqual(element.source_id, "elem_4")
        self.assertEqual(element.label, "plain text")
        # OSA numbers pages from zero; a reader counts from one.
        self.assertEqual(element.page, 2)
        self.assertEqual(next(s for s in claim.sources if s.kind == "quote").page, 2)

    def test_a_referenced_figure_comes_along_with_its_caption(self):
        claim = self._claims(with_layout=True)["c1"]
        reference = next(s for s in claim.sources if s.kind == "reference")
        self.assertEqual(reference.source_id, "elem_9")
        self.assertEqual(reference.label, "Figure 1")
        self.assertEqual(reference.text, "Figure 1: the two towers.")
        self.assertEqual(reference.page, 3)

    def test_evidence_that_matches_nothing_keeps_just_the_quote(self):
        claim = self._claims(with_layout=True)["c2"]
        self.assertEqual([s.kind for s in claim.sources], ["quote"])
        self.assertIsNone(claim.sources[0].page)


class DocumentGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_neighbourhood_of_the_origins_comes_along(self):
        document = load_export(_write_layout(_write_export(self.root))).document
        # elem_4 and elem_9 are origins; elem_10 is one hop out, elem_11 two,
        # elem_12 three - past the edge of what ships.
        self.assertEqual([n.node_id for n in document.nodes],
                         ["elem_10", "elem_11", "elem_4", "elem_9"])
        self.assertIn(("elem_10", "elem_11", "reading_order"),
                      {(e.source, e.target, e.edge_type) for e in document.edges})

    def test_page_structure_is_left_out(self):
        document = load_export(_write_layout(_write_export(self.root))).document
        self.assertNotIn("page_1", {n.node_id for n in document.nodes})
        self.assertNotIn("page_contains", {e.edge_type for e in document.edges})

    def test_element_text_falls_back_to_the_layout(self):
        document = load_export(_write_layout(_write_export(self.root))).document
        caption = next(n for n in document.nodes if n.node_id == "elem_10")
        self.assertEqual(caption.text, "Figure 1: the two towers.")
        self.assertEqual(caption.page, 3)

    def test_no_document_graph_without_one_on_disk(self):
        self.assertIsNone(load_export(_write_export(self.root)).document)


class LoadPaperGraphsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_flat_file_is_read_as_a_paper_graph(self):
        (self.root / "W9.json").write_text(
            json.dumps({
                "publication_id": "W9",
                "sections": [{"section_id": "s001", "name": "Method", "order": 1}],
                "claims": [{"claim_id": "c1", "claim": "A claim.", "section_id": "s001"}],
            }),
            encoding="utf-8",
        )
        graphs = load_paper_graphs(self.root)
        self.assertEqual([g.publication_id for g in graphs], ["W9"])

    def test_a_broken_export_does_not_cost_the_others(self):
        _write_export(self.root, "W1")
        (self.root / "W8.json").write_text("{not json", encoding="utf-8")
        with self.assertLogs("pauk.paper_graph.osa_export", level="WARNING"):
            graphs = load_paper_graphs(self.root)
        self.assertEqual([g.publication_id for g in graphs], ["W1"])

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(load_paper_graphs(self.root / "nope"), [])


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_payload_keeps_claim_files_and_drops_empty_fields(self):
        payload = build_paper_graphs(load_paper_graphs(_write_export(self.root / "src").parent))
        claim = next(c for c in payload["W1"]["claims"] if c["id"] == "c1")
        self.assertEqual(claim["category"], "model_architecture")
        self.assertEqual(claim["files"][0]["url"],
                         "https://github.com/org/repo/blob/HEAD/src/models/encoder.py#L42")
        self.assertNotIn("value", claim)
        self.assertNotIn("contradiction", claim)

    def test_sources_reach_the_payload(self):
        directory = _write_export(self.root / "src")
        _write_layout(directory)
        payload = build_paper_graphs(load_paper_graphs(directory.parent))
        claim = next(c for c in payload["W1"]["claims"] if c["id"] == "c1")
        self.assertEqual([s["kind"] for s in claim["sources"]], ["quote", "element", "reference"])
        self.assertEqual(claim["sources"][1], {"id": "elem_4", "kind": "element",
                                               "page": 2, "label": "plain text"})

    def test_document_graph_reaches_the_payload(self):
        directory = _write_export(self.root / "src")
        _write_layout(directory)
        doc = build_paper_graphs(load_paper_graphs(directory.parent))["W1"]["doc"]
        self.assertEqual(doc["nodes"]["elem_11"], {"name": "plain text", "page": 3,
                                                   "text": "Results follow."})
        self.assertIn(["elem_4", "elem_9", "text_references", "Figure 1"], doc["edges"])

    def test_written_file_defines_the_global_and_counts_what_it_wrote(self):
        counts = write_paper_graphs(_write_export(self.root / "src").parent, self.root / "out")
        self.assertEqual(counts, {"paper_graphs": 1, "paper_claims": 3, "paper_claim_files": 3,
                                  "paper_claim_sources": 2})
        text = (self.root / "out" / "paper-graphs.js").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("window.PAPER_GRAPHS="))
        self.assertIn("_onPaperGraphsReady", text)

    def test_no_exports_still_writes_an_empty_file(self):
        counts = write_paper_graphs(self.root / "nothing-here", self.root / "out")
        self.assertEqual(counts["paper_graphs"], 0)
        self.assertEqual(
            (self.root / "out" / "paper-graphs.js").read_text(encoding="utf-8").split(";")[0],
            "window.PAPER_GRAPHS={}",
        )


if __name__ == "__main__":
    unittest.main()
