import csv
import io
import json
import unittest
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from pauk.similarity.evaluation import _csv_text, create_pool, score_pool


@unittest.skipUnless(find_spec("sklearn"), "similarity extra not installed")
class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "corpus"
        self.index = self.root / "index"
        self.corpus.mkdir()
        self.index.mkdir()
        self.out = self.root / "out"
        self.graph = self.root / "graph.json"
        texts = ["apple fruit", "apple orchard", "ocean whales", "ocean fish", "solar photons", "lunar craters"]
        repos = [{"id": str(i), "url": f"https://github.com/a/{i}", "description": text,
                  "text": text, "text_sha256": sha256(text.encode()).hexdigest(), "skip_reason": None}
                 for i, text in enumerate(texts)]
        corpus = {"schema_version": 1, "snapshot_sha256": "b" * 64, "cleaner_version": 1, "repositories": repos}
        payload = json.dumps(corpus).encode()
        (self.corpus / "manifest.json").write_bytes(payload)
        matrix = np.array([[1, 0], [1, 0], [0, 1], [0, 1], [-1, 0], [0, -1]], dtype=np.float32)
        stream = io.BytesIO()
        np.save(stream, matrix)
        data = stream.getvalue()
        digest = sha256(data).hexdigest()
        (self.index / "matrices").mkdir()
        (self.index / "matrices" / f"{digest}.npy").write_bytes(data)
        idx = {"schema_version": 1, "snapshot_sha256": "b" * 64, "corpus_sha256": sha256(payload).hexdigest(),
               "matrix_sha256": digest, "matrix": f"matrices/{digest}.npy",
               "encoder": {"dimensions": 2, "dtype": "float32", "normalization": "l2"},
               "rows": [{"id": r["id"], "text_sha256": r["text_sha256"]} for r in repos]}
        (self.index / "manifest.json").write_text(json.dumps(idx), encoding="utf8")
        graph = {"repos": [{"key": r["id"]} for r in repos], "repo_edges": [{"s": "0", "t": "1"}]}
        self.graph.write_text(json.dumps(graph), encoding="utf8")

    def test_pool_is_blinded_deterministic_and_preserves_review(self):
        first = create_pool(self.corpus, self.index, self.graph, self.out, sample_size=4, top_k=2)
        other = self.root / "other"
        second = create_pool(self.corpus, self.index, self.graph, other, sample_size=4, top_k=2)
        self.assertEqual(first, second)
        self.assertEqual(first["isolated"], 2)
        self.assertEqual((self.out / "review.csv").read_bytes(), (other / "review.csv").read_bytes())
        review = list(csv.DictReader(io.StringIO((self.out / "review.csv").read_text(encoding="utf-8-sig"))))
        pool = json.loads((self.out / "pool.json").read_bytes())
        self.assertEqual(len(review), first["pairs"])
        self.assertEqual({r["pair_id"] for r in review}, {p["pair_id"] for p in pool["pairs"]})
        self.assertNotIn("score", review[0])
        self.assertNotIn("already_linked", review[0])
        self.assertTrue(any(s["method"] == "tfidf" for p in pool["pairs"] for s in p["sources"]))
        with self.assertRaisesRegex(ValueError, "already exists"):
            create_pool(self.corpus, self.index, self.graph, self.out)

    def test_spreadsheet_formula_prefix_is_escaped(self):
        self.assertEqual(_csv_text("  =HYPERLINK(1)"), "'  =HYPERLINK(1)")
        self.assertEqual(_csv_text("plain text"), "plain text")
        self.assertEqual(_csv_text("a\x00b"), "a\ufffdb")

    def test_large_text_is_flagged_and_limited_for_spreadsheets(self):
        payload = json.loads((self.corpus / "manifest.json").read_bytes())
        text = "apple " * 6000
        payload["repositories"][0]["text"] = text
        digest = sha256(text.encode()).hexdigest()
        payload["repositories"][0]["text_sha256"] = digest
        corpus = json.dumps(payload).encode()
        (self.corpus / "manifest.json").write_bytes(corpus)
        index = json.loads((self.index / "manifest.json").read_bytes())
        index["corpus_sha256"] = sha256(corpus).hexdigest()
        index["rows"][0]["text_sha256"] = digest
        (self.index / "manifest.json").write_text(json.dumps(index), encoding="utf8")
        create_pool(self.corpus, self.index, self.graph, self.out, sample_size=6, top_k=5)
        with (self.out / "review.csv").open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        target = next(r for r in rows if r["repository_a"] == "0" or r["repository_b"] == "0")
        side = "a" if target["repository_a"] == "0" else "b"
        self.assertEqual(target[f"text_truncated_{side}"], "True")
        self.assertEqual(len(target[f"text_{side}"]), 32000)

    def test_rejects_mismatched_graph_and_corpus(self):
        self.graph.write_text('{"repos":[],"repo_edges":[]}', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "repository IDs"):
            create_pool(self.corpus, self.index, self.graph, self.out)
        self.assertFalse(self.out.exists())
        (self.corpus / "manifest.json").write_bytes((self.corpus / "manifest.json").read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "do not match"):
            create_pool(self.corpus, self.index, self.graph, self.out)

    def test_fork_index_is_included_and_mismatched_rows_rejected(self):
        original = json.loads((self.index / "manifest.json").read_bytes())
        fork_dir = self.root / "fork"
        (fork_dir / "matrices").mkdir(parents=True)
        (fork_dir / original["matrix"]).write_bytes((self.index / original["matrix"]).read_bytes())
        corpus = json.loads((self.corpus / "manifest.json").read_bytes())
        original["encoder"]["text_variant"] = "fork"
        for row, source in zip(original["rows"], corpus["repositories"], strict=True):
            row["text_sha256"] = sha256(source["description"].encode()).hexdigest()
        (fork_dir / "manifest.json").write_text(json.dumps(original), encoding="utf8")
        create_pool(self.corpus, self.index, self.graph, self.out, fork_index=fork_dir)
        pool = json.loads((self.out / "pool.json").read_bytes())
        self.assertIn("fork_dense", pool["methods"])
        self.assertTrue(any(s["method"] == "fork_dense" for p in pool["pairs"] for s in p["sources"]))
        original["rows"][0]["text_sha256"] = "f" * 64
        (fork_dir / "manifest.json").write_text(json.dumps(original), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "fork index rows"):
            create_pool(self.corpus, self.index, self.graph, self.root / "bad", fork_index=fork_dir)
        original["rows"][0]["text_sha256"] = sha256(corpus["repositories"][0]["description"].encode()).hexdigest()
        original["encoder"]["max_tokens"] = 100
        (fork_dir / "manifest.json").write_text(json.dumps(original), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "same encoder settings"):
            create_pool(self.corpus, self.index, self.graph, self.root / "bad", fork_index=fork_dir)

    def test_score_uses_only_new_retrievals_with_judged_duplicate_status(self):
        create_pool(self.corpus, self.index, self.graph, self.out, sample_size=6, top_k=2)
        review = self.out / "review.csv"
        with review.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames
            rows = list(reader)
        for row in rows:
            row["judgment"] = "same_function"
            row["duplicate_or_fork"] = "no"
        with review.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        report = self.root / "metrics.json"
        results = score_pool(self.out / "pool.json", review, report)
        for value in results.values():
            self.assertGreater(value["eligible"], 0)
            self.assertEqual(value["precision"], 1)
            self.assertLess(value["wilson_95"][0], 1)
        with self.assertRaisesRegex(ValueError, "already exists"):
            score_pool(self.out / "pool.json", review, report)
        rows[0]["judgment"] = "invented"
        with review.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "invalid"):
            score_pool(self.out / "pool.json", review, self.root / "bad.json")
