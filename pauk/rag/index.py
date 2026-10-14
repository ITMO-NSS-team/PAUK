"""Building one index profile in Neo4j.

For every node of the profile's labels: compose its text from the
profile's fields, embed it, store text + vector + hash, then create a
vector index and a full-text (BM25) index on those properties. The hash
covers the model and the text, so rebuilding after `pauk publish` embeds
only nodes whose text actually changed - and a profile can be rebuilt as
often as an ablation needs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from pauk.settings import Settings

from . import readme
from .embedder import Embedder
from .graph import Graph
from .profiles import Profile

logger = logging.getLogger(__name__)

WRITE_BATCH = 200
# Texts are mostly English. On a raw Russian question no analyzer helps BM25
# (compared on 2026-10-06: standard, english and russian return the same
# noise) - there are hardly any shared words. With the question translated
# (`Rag.translate`, passed to retrieve as `bm25_query`), English stemming and stop words are what matter.
ANALYZER = "english"
NOT_RESEARCH = {"paratext", "peer-review"}
FIGURE_LIKE = {"other", "dataset"}  # without an abstract: almost always one figure or table with its own DOI


def is_research(row: dict[str, Any]) -> bool:
    if row.get("type") in NOT_RESEARCH:
        return False
    return bool((row.get("abstract") or "").strip()) or row.get("type") not in FIGURE_LIKE


def text_hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\0{text}".encode()).hexdigest()


class VectorCache:
    """Vectors by text hash, shared by all profiles on disk.

    Profiles that differ only in repository fields share every publication
    text; without the cache each ablation would re-embed 13 thousand
    abstracts (minutes on a GPU) to get the same vectors back.
    """

    def __init__(self, config: Settings, model: str) -> None:
        slug = hashlib.sha1(model.encode()).hexdigest()[:12]
        self.path = config.data_dir / "rag" / "vectors" / f"{slug}.npz"
        self.vectors: dict[str, np.ndarray] = {}
        if self.path.is_file():
            with np.load(self.path) as data:
                self.vectors = dict(zip(data["keys"].tolist(), data["vectors"], strict=True))
        self.added = 0

    def get(self, key: str) -> np.ndarray | None:
        return self.vectors.get(key)

    def put(self, key: str, vector: np.ndarray) -> None:
        self.vectors[key] = vector
        self.added += 1

    def save(self) -> None:
        if not self.added:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(self.vectors)
        tmp = self.path.with_suffix(".tmp.npz")
        np.savez(tmp, keys=np.array(keys), vectors=np.stack([self.vectors[k] for k in keys]))
        tmp.replace(self.path)


def manifest_path(config: Settings, name: str) -> Path:
    return config.data_dir / "rag" / "profiles" / f"{name}.json"


def load_manifest(config: Settings, name: str) -> dict[str, Any] | None:
    path = manifest_path(config, name)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def build(config: Settings, profile: Profile, *, force: bool = False, embedder: Embedder | None = None,
          graph: Graph | None = None) -> dict[str, Any]:
    model = config.rag_embedding_model
    embedder = embedder or Embedder(model)
    own_graph = graph is None
    graph = graph or Graph(config)
    stats: dict[str, Any] = {}
    cache = VectorCache(config, model)
    try:
        for label in profile.labels():
            stats[label] = _build_label(config, graph, embedder, cache, profile, label, model, force)
        graph.await_indexes()
    finally:
        cache.save()
        if own_graph:
            graph.close()
    manifest = {
        "profile": profile.to_dict(),
        "embedding_model": model,
        "analyzer": ANALYZER,
        "built_at": datetime.now(UTC).isoformat(),
        "labels": stats,
    }
    path = manifest_path(config, profile.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def _build_label(config: Settings, graph: Graph, embedder: Embedder, cache: VectorCache, profile: Profile,
                 label: str, model: str, force: bool) -> dict[str, int]:
    rows = graph.nodes(label, profile)
    if label == "Repository":
        for row in rows:
            row["readme"] = readme.load_readme(config, row["id"])
    todo, unchanged, cleared = [], 0, []
    skipped = 0
    for row in rows:
        if label == "Publication" and profile.skip_non_research and not is_research(row):
            skipped += 1
            text = ""
        else:
            text = profile.compose(label, row)
        if not text:
            if row.get("hash"):
                cleared.append(row["id"])
            continue
        digest = text_hash(model, text)
        if digest == row.get("hash") and not force:
            unchanged += 1
            continue
        todo.append({"id": row["id"], "text": text, "hash": digest})
    logger.info("%s / %s: %d to write, %d unchanged, %d cleared, %d not research",
                profile.name, label, len(todo), unchanged, len(cleared), skipped)
    
    missing = [row for row in todo if force or cache.get(row["hash"]) is None]

    if missing:
        vectors = embedder.encode([row["text"] for row in missing], progress=True)
        for row, vector in zip(missing, vectors, strict=True):
            cache.put(row["hash"], vector)

    for row in todo:
        row["vector"] = cache.get(row["hash"]).tolist()

    if todo:
        for start in range(0, len(todo), WRITE_BATCH):
            graph.store(label, profile, todo[start:start + WRITE_BATCH])
    
    if cleared:
        graph.clear(label, profile, cleared)
    
    graph.ensure_indexes(label, profile, embedder.dimensions, ANALYZER)
    return {"nodes": len(rows), "written": len(todo), "embedded": len(missing), "unchanged": unchanged,
            "cleared": len(cleared),
            "not_research": skipped, "indexed": len(todo) + unchanged}
