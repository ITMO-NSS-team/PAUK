"""Rank repository pairs from a validated, immutable embedding index."""

import io
import json
import math
import re
from hashlib import sha256
from pathlib import Path

import numpy as np

from pauk.storage.atomic import atomic_write_bytes

from .index import _json


def _load_index(index: Path) -> tuple[dict, np.ndarray, str, Path]:
    manifest_path = index / "manifest.json" if index.is_dir() else index
    root = manifest_path.parent.parent if manifest_path.parent.name == "versions" else manifest_path.parent
    payload = manifest_path.read_bytes()
    manifest = json.loads(payload)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported index schema")
    rows = manifest.get("rows")
    encoder = manifest.get("encoder")
    if not isinstance(rows, list) or not isinstance(encoder, dict):
        raise ValueError("invalid index rows or encoder")
    dimensions = encoder.get("dimensions")
    if type(dimensions) is not int or dimensions < 1 or encoder.get("normalization") != "l2":
        raise ValueError("invalid index dimensions or normalization")
    if encoder.get("dtype") != "float32":
        raise ValueError("unsupported index dtype")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("invalid index repository ID")
        if row["id"] in seen:
            raise ValueError(f"duplicate repository ID: {row['id']}")
        seen.add(row["id"])
        if not isinstance(row.get("text_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["text_sha256"]):
            raise ValueError(f"invalid text digest for {row['id']}")
    for key in ("corpus_sha256", "snapshot_sha256", "matrix_sha256"):
        if not isinstance(manifest.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", manifest[key]):
            raise ValueError(f"invalid index {key}")
    expected_path = f"matrices/{manifest['matrix_sha256']}.npy"
    if manifest.get("matrix") != expected_path:
        raise ValueError("invalid index matrix path")
    matrix_bytes = (root / expected_path).read_bytes()
    if sha256(matrix_bytes).hexdigest() != manifest["matrix_sha256"]:
        raise ValueError("index matrix digest mismatch")
    try:
        matrix = np.load(io.BytesIO(matrix_bytes), allow_pickle=False)
    except (ValueError, EOFError) as exc:
        raise ValueError("invalid index matrix file") from exc
    if not isinstance(matrix, np.ndarray) or matrix.dtype != np.dtype("float32") or matrix.shape != (len(rows), dimensions):
        raise ValueError("index matrix shape or dtype does not match manifest")
    if not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix.astype(np.float64), axis=1), 1, atol=1e-5):
        raise ValueError("index matrix contains invalid or non-normalized vectors")
    return manifest, matrix, sha256(payload).hexdigest(), root


def build_neighbors(index: Path, out: Path, *, top_k: int, min_score: float = -1.0,
                    mutual: bool = False) -> dict:
    """Keep directed top-K candidates, and select undirected pairs using a threshold."""
    if top_k < 1:
        raise ValueError("top-k must be positive")
    if not math.isfinite(min_score) or not -1 <= min_score <= 1:
        raise ValueError("min-score must be finite and in [-1, 1]")
    manifest, matrix, index_digest, root = _load_index(index)
    if out.resolve() == root.resolve():
        raise ValueError("neighbors output must be separate from the index")
    rows = manifest["rows"]
    ids = [row["id"] for row in rows]
    by_id = {row["id"]: row["text_sha256"] for row in rows}
    # Compute in float64 to avoid losing precision when comparing close candidates.
    scores = matrix.astype(np.float64) @ matrix.astype(np.float64).T
    scores = np.clip(scores, -1.0, 1.0)
    directed = {}
    for i, rid in enumerate(ids):
        ordered = sorted((j for j in range(len(ids)) if j != i), key=lambda j: (-scores[i, j], ids[j]))
        directed[rid] = [{"id": ids[j], "score": float(scores[i, j]), "rank": rank}
                         for rank, j in enumerate(ordered[:top_k], start=1)]
    ranks = {(rid, item["id"]): item["rank"] for rid, items in directed.items() for item in items}
    pairs = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = sorted((ids[i], ids[j]))
            score = float(scores[i, j])
            ab, ba = ranks.get((a, b)), ranks.get((b, a))
            selected = (ab is not None and ba is not None) if mutual else (ab is not None or ba is not None)
            if score >= min_score and selected:
                pairs.append({"a": a, "b": b, "score": score, "rank_a": ab, "rank_b": ba,
                              "text_sha256_a": by_id[a], "text_sha256_b": by_id[b]})
    pairs.sort(key=lambda row: (-row["score"], row["a"], row["b"]))
    result = {"schema_version": 1, "index_sha256": index_digest, "corpus_sha256": manifest["corpus_sha256"],
              "snapshot_sha256": manifest["snapshot_sha256"], "top_k": top_k, "min_score": min_score,
              "mutual": mutual, "candidates": {rid: directed[rid] for rid in sorted(ids)}, "pairs": pairs}
    payload = _json(result)
    version = sha256(payload).hexdigest()
    atomic_write_bytes(out / "versions" / f"{version}.json", payload)
    atomic_write_bytes(out / "manifest.json", payload)
    return {"version": version, "repositories": len(ids), "pairs": len(pairs)}
