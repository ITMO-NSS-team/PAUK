import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np

from pauk.similarity.index import DIMENSIONS, build_index


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.out = self.root / "index"
        self.model = Mock()
        self.model.encode.side_effect = lambda texts, **kw: np.ones((len(texts), DIMENSIONS), dtype=np.float32)

    def write_corpus(self, texts):
        rows = [{"id": key, "text": text, "text_sha256": sha256(text.encode()).hexdigest() if text else None,
                 "skip_reason": None if text else "no_descriptive_text"} for key, text in texts]
        data = {"schema_version": 1, "snapshot_sha256": "a" * 64, "cleaner_version": 1, "repositories": rows}
        (self.corpus / "manifest.json").write_text(json.dumps(data), encoding="utf8")

    def build(self, **kwargs):
        with patch("pauk.similarity.index._load_encoder", return_value=self.model) as loader:
            result = build_index(self.corpus, self.out, **kwargs)
        return result, loader

    def test_sorted_rows_empty_and_cache_reuse(self):
        self.write_corpus([("z", "one"), ("a", "two"), ("skip", "")])
        result, _ = self.build(batch_size=1)
        manifest = json.loads((self.out / "manifest.json").read_bytes())
        self.assertEqual([r["id"] for r in manifest["rows"]], ["a", "z"])
        self.assertEqual(result["skipped"], 1)
        matrix = np.load(self.out / manifest["matrix"], allow_pickle=False)
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1)
        result2, loader = self.build()
        loader.assert_not_called()
        self.assertEqual(result2["version"], result["version"])
        self.assertEqual(result2["cached_texts"], 2)
        self.write_corpus([])
        self.build()
        manifest = json.loads((self.out / "manifest.json").read_bytes())
        self.assertEqual(np.load(self.out / manifest["matrix"]).shape, (0, DIMENSIONS))

    def test_bad_corpus_and_vectors_rejected(self):
        self.write_corpus([("same", "one"), ("same", "two")])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.build()
        self.write_corpus([("one", "one")])
        for bad in [np.zeros((1, DIMENSIONS)), np.full((1, DIMENSIONS), np.nan), np.ones((1, 2))]:
            self.model.encode.return_value = bad
            self.model.encode.side_effect = None
            with self.subTest(shape=bad.shape), self.assertRaises(ValueError):
                self.build()
        self.assertFalse((self.out / "manifest.json").exists())

    def test_interruption_preserves_manifest_and_recovers_batches(self):
        self.write_corpus([("a", "one")])
        self.build(batch_size=1)
        old = (self.out / "manifest.json").read_bytes()
        self.write_corpus([("a", "one"), ("b", "two"), ("c", "three")])
        self.model.encode.side_effect = [np.ones((1, DIMENSIONS)), RuntimeError("interrupted")]
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.build(batch_size=1)
        self.assertEqual((self.out / "manifest.json").read_bytes(), old)
        self.model.encode.side_effect = None
        self.model.encode.return_value = np.ones((1, DIMENSIONS))
        result, _ = self.build(batch_size=1)
        self.assertEqual(result["encoded_texts"], 1)
        self.assertEqual(result["cached_texts"], 2)

    def test_corrupted_cache_recomputed_and_same_output_rejected(self):
        self.write_corpus([("a", "one")])
        self.build()
        cache = next((self.out / "cache").glob("*/*"))
        cache.write_bytes(b"invalid")
        result, _ = self.build()
        self.assertEqual(result["encoded_texts"], 1)
        with self.assertRaises(ValueError):
            build_index(self.corpus, self.corpus)

    def test_fork_variant_uses_verified_raw_text_and_distinct_cache(self):
        self.write_corpus([("a", "cleaned content")])
        path = self.corpus / "manifest.json"
        data = json.loads(path.read_bytes())
        body = b"raw README content"
        digest = sha256(body).hexdigest()
        (self.corpus / "blobs").mkdir()
        blob = self.corpus / "blobs" / digest
        blob.write_bytes(body)
        data["repositories"][0].update({"name": "project", "description": "sample", "topics": ["topic"],
                                       "readme": {"blob": "blobs/" + digest}})
        path.write_text(json.dumps(data), encoding="utf8")
        self.build()
        result, _ = self.build(text_variant="fork")
        self.assertEqual(result["encoded_texts"], 1)
        self.assertEqual(self.model.encode.call_args.args[0], ["project. sample. topic. raw README content"])
        blob.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.build(text_variant="fork")
