"""Create a blinded review pool from dense and TF-IDF repository rankings."""

import csv
import io
import json
import math
import random
import re
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import numpy as np

from pauk.storage.atomic import atomic_write_bytes

from .index import _fork_texts, _json, _read_corpus
from .neighbors import _load_index


def _csv_text(value: str) -> str:
    """Keep repository-controlled strings from becoming spreadsheet formulas."""
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "\ufffd", value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


def create_pool(corpus: Path, index: Path, graph: Path, out: Path, *, sample_size: int = 60,
                top_k: int = 3, seed: int = 42, fork_index: Path | None = None) -> dict:
    """Persist a blinded CSV and its separate ranking metadata for subsequent scoring."""
    if sample_size < 1 or top_k < 1:
        raise ValueError("sample-size and top-k must be positive")
    if (out / "review.csv").exists():
        raise ValueError("review.csv already exists; choose a new output directory to preserve judgments")
    corpus_file = corpus / "manifest.json" if corpus.is_dir() else corpus
    provenance, documents, texts, _ = _read_corpus(corpus_file)
    idx, vectors, index_digest, index_root = _load_index(index)
    if out.resolve() in {index_root.resolve(), corpus_file.parent.resolve()}:
        raise ValueError("evaluation output must be separate from corpus and index")
    if idx["corpus_sha256"] != provenance["corpus_sha256"] or idx["snapshot_sha256"] != provenance["snapshot_sha256"]:
        raise ValueError("corpus and index do not match")
    by_id = {doc.id: doc for doc in documents}
    rows = idx["rows"]
    if {r["id"]: r["text_sha256"] for r in rows} != {d.id: d.text_sha256 for d in documents}:
        raise ValueError("index rows do not match corpus")
    corpus_data = json.loads(corpus_file.read_bytes())
    repo_data = {r["id"]: r for r in corpus_data["repositories"]}
    corpus_root = corpus_file.parent.parent if corpus_file.parent.name == "versions" else corpus_file.parent
    graph_bytes = graph.read_bytes()
    graph_data = json.loads(graph_bytes)
    graph_ids = {r["key"] for r in graph_data["repos"]}
    if graph_ids != set(repo_data):
        raise ValueError("graph repository IDs differ from the corpus")
    linked = {tuple(sorted((edge["s"], edge["t"]))) for edge in graph_data["repo_edges"]}
    if any(a not in graph_ids or b not in graph_ids or a == b for a, b in linked):
        raise ValueError("graph contains invalid repository edges")
    ids = [r["id"] for r in rows]
    if len(ids) < 2:
        raise ValueError("at least two prepared repositories are required")
    connected = {node for edge in linked for node in edge}
    isolated = sorted(set(ids) - connected)
    others = sorted(set(ids) & connected)
    rng = random.Random(seed)
    rng.shuffle(isolated)
    rng.shuffle(others)
    target = min(sample_size, len(ids))
    n_isolated = min(len(isolated), (target + 1) // 2)
    n_other = min(len(others), target - n_isolated)
    n_isolated = min(len(isolated), target - n_other)
    selected = sorted(isolated[:n_isolated] + others[:n_other])
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError as exc:
        raise RuntimeError("install the embedding dependencies with 'uv sync --extra similarity'") from exc
    corpus_texts = [texts[by_id[rid].text_sha256] for rid in ids]
    tfidf = TfidfVectorizer(sublinear_tf=True, max_df=0.35, ngram_range=(1, 2))
    try:
        lexical = tfidf.fit_transform(corpus_texts)
    except ValueError as exc:
        raise ValueError(f"TF-IDF could not build a vocabulary: {exc}") from exc
    dense_scores = vectors.astype(np.float64) @ vectors.astype(np.float64).T
    lexical_scores = (lexical @ lexical.T).toarray()
    methods = [("dense", dense_scores), ("tfidf", lexical_scores)]
    fork_digest = None
    if fork_index:
        fork, fork_vectors, fork_digest, _ = _load_index(fork_index)
        if fork["corpus_sha256"] != provenance["corpus_sha256"] or fork["snapshot_sha256"] != provenance["snapshot_sha256"]:
            raise ValueError("fork index and corpus do not match")
        comparable = ("model", "revision", "max_tokens", "dimensions", "dtype", "normalization", "runtime")
        if any(fork["encoder"].get(key) != idx["encoder"].get(key) for key in comparable):
            raise ValueError("fork and cleaned indexes must use the same encoder settings")
        fork_docs, _ = _fork_texts(corpus_file, documents)
        expected = {d.id: d.text_sha256 for d in fork_docs}
        if fork["encoder"].get("text_variant") != "fork" or {r["id"]: r["text_sha256"] for r in fork["rows"]} != expected:
            raise ValueError("fork index rows do not match fork-style corpus texts")
        fork_positions = {row["id"]: i for i, row in enumerate(fork["rows"])}
        ordered_fork = fork_vectors[[fork_positions[rid] for rid in ids]].astype(np.float64)
        methods.append(("fork_dense", ordered_fork @ ordered_fork.T))
    positions = {rid: i for i, rid in enumerate(ids)}
    pooled: dict[tuple[str, str], dict] = {}
    for rid in selected:
        i = positions[rid]
        for method, scores in methods:
            candidates = sorted((j for j in range(len(ids)) if j != i and (method != "tfidf" or scores[i, j] > 0)),
                                key=lambda j: (-scores[i, j], ids[j]))
            for rank, j in enumerate(candidates[:top_k], 1):
                pair = tuple(sorted((rid, ids[j])))
                info = pooled.setdefault(pair, {"sources": []})
                info["sources"].append({"method": method, "query": rid, "rank": rank, "score": float(scores[i, j])})
    pairs = sorted(pooled)
    rng.shuffle(pairs)
    review = io.StringIO(newline="")
    fields = ["pair_id", "repository_a", "url_a", "description_a", "text_a", "text_truncated_a",
              "readme_path_a", "readme_sha_a", "readme_a", "readme_truncated_a",
              "repository_b", "url_b", "description_b", "text_b", "text_truncated_b",
              "readme_path_b", "readme_sha_b", "readme_b", "readme_truncated_b",
              "judgment", "shared_function", "duplicate_or_fork", "comment"]
    writer = csv.DictWriter(review, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    metadata = []
    raw_readmes: dict[str, str] = {}
    for a, b in pairs:
        pair_id = sha256(_json([a, b])).hexdigest()
        record = {"pair_id": pair_id}
        for side, rid in (("a", a), ("b", b)):
            repo = repo_data[rid]
            full_text = texts[by_id[rid].text_sha256]
            readme = repo.get("readme") or {}
            blob = readme.get("blob")
            if blob:
                if not isinstance(blob, str) or not re.fullmatch(r"blobs/[0-9a-f]{64}", blob):
                    raise ValueError(f"invalid README blob for {rid}")
                if blob not in raw_readmes:
                    body = (corpus_root / blob).read_bytes()
                    if sha256(body).hexdigest() != blob.removeprefix("blobs/"):
                        raise ValueError(f"README blob digest mismatch for {rid}")
                    raw_readmes[blob] = body.decode("utf-8", errors="replace")
            raw_text = raw_readmes.get(blob, "")
            record.update({f"repository_{side}": _csv_text(rid), f"url_{side}": _csv_text(repo.get("url") or ""),
                           f"description_{side}": _csv_text(repo.get("description") or ""),
                           f"text_{side}": _csv_text(full_text[:32000]),
                           f"text_truncated_{side}": len(full_text) > 32000,
                           f"readme_path_{side}": _csv_text(readme.get("path") or ""),
                           f"readme_sha_{side}": _csv_text(readme.get("sha") or ""),
                           f"readme_{side}": _csv_text(raw_text[:32000]),
                           f"readme_truncated_{side}": len(raw_text) > 32000})
        writer.writerow(record)
        metadata.append({"pair_id": pair_id, "a": a, "b": b, "already_linked": (a, b) in linked,
                         **pooled[(a, b)]})
    data = {"schema_version": 1, "corpus_sha256": provenance["corpus_sha256"], "index_sha256": index_digest,
            "snapshot_sha256": provenance["snapshot_sha256"], "graph_sha256": sha256(graph_bytes).hexdigest(),
            "seed": seed, "top_k": top_k, "sample": selected, "isolated_sample": n_isolated,
            "fork_index_sha256": fork_digest, "methods": [name for name, _ in methods],
            "tfidf": {"sublinear_tf": True, "max_df": 0.35, "ngram_range": [1, 2],
                      "scikit_learn_version": version("scikit-learn")}, "pairs": metadata}
    atomic_write_bytes(out / "pool.json", _json(data))
    atomic_write_bytes(out / "review.csv", ("\ufeff" + review.getvalue()).encode("utf-8"))
    return {"sample": len(selected), "isolated": n_isolated, "pairs": len(pairs)}


def score_pool(pool: Path, review: Path, output: Path) -> dict:
    """Summarize judgments only for genuinely retrieved, previously unlinked pairs."""
    if output.exists():
        raise ValueError("output already exists; choose a new path")
    metadata = json.loads(pool.read_bytes())
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1 or not isinstance(metadata.get("pairs"), list):
        raise ValueError("invalid pool metadata")
    with review.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or not {"pair_id", "judgment", "duplicate_or_fork"}.issubset(reader.fieldnames):
            raise ValueError("review CSV is missing required columns")
        labels = {}
        allowed = {"same_function", "related", "unrelated", "insufficient"}
        for row in reader:
            key = row["pair_id"]
            judgment = row["judgment"].strip().lower()
            duplicate = row["duplicate_or_fork"].strip().lower()
            if key in labels or judgment not in allowed | {""} or duplicate not in {"", "yes", "no"}:
                raise ValueError(f"invalid or duplicate judgment for {key}")
            labels[key] = (judgment, duplicate)
    pairs = metadata["pairs"]
    ids = [pair["pair_id"] for pair in pairs]
    if len(ids) != len(set(ids)) or set(ids) != set(labels):
        raise ValueError("review IDs do not match the pool")
    metrics = {}
    for method in metadata.get("methods", ["dense", "tfidf"]):
        retrieved = [pair for pair in pairs if not pair["already_linked"]
                     and any(source["method"] == method for source in pair["sources"])]
        judged = [pair for pair in retrieved if labels[pair["pair_id"]][0]]
        unknown_duplicate = sum(labels[p["pair_id"]][1] == "" for p in judged)
        eligible = [pair for pair in judged if labels[pair["pair_id"]][1] == "no"]
        positive = sum(labels[pair["pair_id"]][0] == "same_function" for pair in eligible)
        total = len(eligible)
        interval = None
        if total:
            z = 1.959963984540054
            fraction = positive / total
            denominator = 1 + z * z / total
            center = (fraction + z * z / (2 * total)) / denominator
            margin = z * math.sqrt(fraction * (1 - fraction) / total + z * z / (4 * total * total)) / denominator
            interval = [max(0.0, center - margin), min(1.0, center + margin)]
        metrics[method] = {"new_retrieved": len(retrieved), "judged": len(judged),
                           "awaiting_duplicate_decision": unknown_duplicate, "eligible": total,
                           "same_function": positive, "precision": positive / total if total else None,
                           "wilson_95": interval}
    report = {"schema_version": 1, "pool_sha256": sha256(pool.read_bytes()).hexdigest(),
              "review_sha256": sha256(review.read_bytes()).hexdigest(), "metrics": metrics}
    atomic_write_bytes(output, _json(report))
    return metrics
