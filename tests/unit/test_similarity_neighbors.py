import io
import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from pauk.cli import _build_parser, _dispatch
from pauk.similarity.neighbors import build_neighbors


class NeighborsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.index = self.root / "index"
        self.index.mkdir()
        self.out = self.root / "neighbors"

    def make_index(self, vectors):
        rows = [{"id": rid, "text_sha256": sha256(rid.encode()).hexdigest()} for rid, _ in vectors]
        matrix = np.array([vector for _, vector in vectors], dtype=np.float32).reshape((-1, 2))
        stream = io.BytesIO()
        np.save(stream, matrix)
        data = stream.getvalue()
        digest = sha256(data).hexdigest()
        path = self.index / "matrices" / f"{digest}.npy"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        manifest = {"schema_version": 1, "corpus_sha256": "a" * 64, "snapshot_sha256": "b" * 64,
                    "matrix_sha256": digest, "matrix": f"matrices/{digest}.npy", "rows": rows,
                    "encoder": {"dimensions": 2, "dtype": "float32", "normalization": "l2"}}
        (self.index / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def load(self):
        return json.loads((self.out / "manifest.json").read_bytes())

    def test_ties_rank_by_id_and_union_differs_from_mutual(self):
        self.make_index([("c", (1, 0)), ("b", (1, 0)), ("a", (1, 0)), ("d", (-1, 0))])
        build_neighbors(self.index, self.out, top_k=1, min_score=1)
        result = self.load()
        self.assertEqual([item["id"] for item in result["candidates"]["c"]], ["a"])
        self.assertEqual([(p["a"], p["b"]) for p in result["pairs"]], [("a", "b"), ("a", "c")])
        self.assertIsNone(result["pairs"][1]["rank_a"])
        build_neighbors(self.index, self.out, top_k=1, min_score=1, mutual=True)
        self.assertEqual([(p["a"], p["b"]) for p in self.load()["pairs"]], [("a", "b")])

    def test_negative_score_boundary_large_k_and_permutation(self):
        vectors = [("b", (1, 0)), ("a", (-1, 0)), ("c", (0, 1))]
        self.make_index(vectors)
        build_neighbors(self.index, self.out, top_k=9, min_score=-1)
        first = self.load()
        self.assertEqual(len(first["pairs"]), 3)
        self.assertEqual(first["pairs"][-1]["score"], -1)
        self.make_index(list(reversed(vectors)))
        build_neighbors(self.index, self.out, top_k=9, min_score=-1)
        second = self.load()
        self.assertEqual(first["pairs"], second["pairs"])
        self.assertEqual(first["candidates"], second["candidates"])

    def test_empty_single_and_invalid_arguments(self):
        for rows in ([], [("one", (1, 0))]):
            self.make_index(rows)
            build_neighbors(self.index, self.out, top_k=1)
            self.assertEqual(self.load()["pairs"], [])
        for kwargs in ({"top_k": 0}, {"top_k": 1, "min_score": float("nan")},
                       {"top_k": 1, "min_score": -1.1}):
            with self.assertRaises(ValueError):
                build_neighbors(self.index, self.out, **kwargs)

    def test_corrupt_matrix_invalid_ids_and_output_do_not_replace_result(self):
        path, manifest = self.make_index([("a", (1, 0)), ("b", (0, 1))])
        build_neighbors(self.index, self.out, top_k=1)
        old = (self.out / "manifest.json").read_bytes()
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "digest"):
            build_neighbors(self.index, self.out, top_k=1)
        self.assertEqual(old, (self.out / "manifest.json").read_bytes())
        manifest["rows"][1]["id"] = "a"
        (self.index / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_neighbors(self.index, self.out, top_k=1)
        self.make_index([("a", (1, 0))])
        with self.assertRaisesRegex(ValueError, "separate"):
            build_neighbors(self.index, self.index, top_k=1)

    def test_cli(self):
        self.make_index([("b", (1, 0)), ("a", (0, 1))])
        parser = _build_parser()
        args = parser.parse_args(["similarity", "neighbors", "--index", str(self.index), "--out", str(self.out),
                                  "--top-k", "1", "--mutual", "--min-score", "0"])
        _dispatch(args, parser)
        self.assertEqual(self.load()["pairs"][0]["score"], 0)

    def test_matrix_validation_and_version_manifest(self):
        path, manifest = self.make_index([("a", (1, 0)), ("b", (0, 1))])
        version_dir = self.index / "versions"
        version_dir.mkdir()
        version_path = version_dir / "chosen.json"
        version_path.write_text(json.dumps(manifest), encoding="utf8")
        build_neighbors(version_path, self.out, top_k=1)
        self.assertEqual(len(self.load()["pairs"]), 1)
        for array in (np.array([[1, 0]], dtype=np.float32), np.array([[1, 0], [0, np.nan]], dtype=np.float32),
                      np.array([[2, 0], [0, 1]], dtype=np.float32)):
            stream = io.BytesIO()
            np.save(stream, array)
            data = stream.getvalue()
            digest = sha256(data).hexdigest()
            path = self.index / "matrices" / f"{digest}.npy"
            path.write_bytes(data)
            manifest["matrix"] = f"matrices/{digest}.npy"
            manifest["matrix_sha256"] = digest
            version_path.write_text(json.dumps(manifest), encoding="utf8")
            with self.subTest(array=array), self.assertRaises(ValueError):
                build_neighbors(version_path, self.out, top_k=1)
