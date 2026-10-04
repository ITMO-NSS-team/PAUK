"""Finding entry points into the graph by meaning and by words.

Three modes, kept side by side because comparing them is part of the
evaluation: `bm25` (word overlap), `dense` (embedding cosine) and `hybrid`
(a weighted sum of both, each min-max normalised over the retrieved set).

The relevance a hit carries downstream is normalised within its result list.
bge-m3 cosines sit in a narrow band (about 0.52-0.66 over the top 300), so
raw cosines would weigh the 150th hit almost like the first; spreading them
over [0, 1] lets a person with one strong paper outrank one with many weak ones.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from rank_bm25 import BM25Okapi

Mode = Literal["bm25", "dense", "hybrid"]
MODES: tuple[Mode, ...] = ("bm25", "dense", "hybrid")
DENSE_WEIGHT = 0.7  # hybrid = DENSE_WEIGHT · dense + (1 - DENSE_WEIGHT) · bm25

_TOKEN = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [token for token in _TOKEN.findall(text.casefold().replace("ё", "е")) if len(token) > 1]


def min_max(scored: list[tuple[str, float]]) -> dict[str, float]:
    """Spread one result list over [0, 1]; a single hit, or all equal, counts as 1."""
    if not scored:
        return {}
    values = [score for _, score in scored]
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return {doc_id: 1.0 for doc_id, _ in scored}
    return {doc_id: (score - low) / (high - low) for doc_id, score in scored}


def pool(query_vectors: np.ndarray) -> np.ndarray:
    """Several key phrases become one query: their normalised mean.

    Taking the best match per phrase instead lets the most ambiguous phrase win -
    "диффузионные модели" alone pulls in ion diffusion and mass transfer.
    """
    mean = query_vectors.mean(axis=0)
    norm = np.linalg.norm(mean)
    return (mean / norm if norm else mean)[None, :].astype(np.float32)


@dataclass(frozen=True)
class Hit:
    id: str
    score: float  # relevance in [0, 1] used downstream as a weight
    dense: float | None = None
    bm25: float | None = None


class Collection:
    """One searchable node type: ids, their texts and their vectors."""

    def __init__(self, ids: list[str], texts: list[str], vectors: np.ndarray) -> None:
        self.ids = ids
        self.position = {doc_id: i for i, doc_id in enumerate(ids)}
        self.vectors = vectors
        self.bm25 = BM25Okapi([tokenize(text) for text in texts]) if texts else None

    @classmethod
    def load(cls, data_dir: Path, name: str) -> Collection:
        rows = json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8"))
        vectors = np.load(data_dir / f"{name}.npy")
        return cls([row["id"] for row in rows], [row["text"] for row in rows], vectors)

    def _subset(self, allowed: set[str] | None) -> np.ndarray:
        if allowed is None:
            return np.arange(len(self.ids))
        return np.array(sorted(self.position[i] for i in allowed if i in self.position), dtype=int)

    def dense(self, query_vectors: np.ndarray, k: int, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        rows = self._subset(allowed)
        if not len(rows) or not len(query_vectors):
            return []
        scores = (self.vectors[rows] @ pool(query_vectors).T)[:, 0]
        order = np.argsort(-scores)[:k]
        return [(self.ids[rows[i]], float(scores[i])) for i in order]

    def lexical(self, query: str, k: int, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        tokens = tokenize(query)
        rows = self._subset(allowed)
        if self.bm25 is None or not tokens or not len(rows):
            return []
        # get_scores is vectorised over the whole corpus; subsetting after is cheaper
        # than rank_bm25's per-document get_batch_scores loop.
        scores = np.asarray(self.bm25.get_scores(tokens))[rows]
        order = [i for i in np.argsort(-scores)[:k] if scores[i] > 0]
        return [(self.ids[rows[i]], float(scores[i])) for i in order]

    def search(self, mode: Mode, phrases: list[str], embed: Callable[[list[str]], np.ndarray] | None,
               k: int, allowed: set[str] | None = None) -> list[Hit]:
        if mode not in MODES:
            raise ValueError(f"unknown retrieval mode {mode!r}")
        dense = self.dense(embed(phrases), k, allowed) if mode != "bm25" and embed and phrases else []
        lexical = self.lexical(" ".join(phrases), k, allowed) if mode != "dense" else []
        dense_norm, lexical_norm = min_max(dense), min_max(lexical)
        dense_raw, lexical_raw = dict(dense), dict(lexical)
        if mode == "dense":
            combined = dense_norm
        elif mode == "bm25":
            combined = lexical_norm
        else:
            combined = {
                doc_id: DENSE_WEIGHT * dense_norm.get(doc_id, 0.0) + (1 - DENSE_WEIGHT) * lexical_norm.get(doc_id, 0.0)
                for doc_id in dense_norm.keys() | lexical_norm.keys()
            }
        ranked = sorted(combined.items(), key=lambda item: -item[1])[:k]
        return [Hit(doc_id, score, dense=dense_raw.get(doc_id), bm25=lexical_raw.get(doc_id))
                for doc_id, score in ranked]


# bge-m3 accepts 8192 tokens; long READMEs at that length exhaust an 8 GB GPU.
# 512 tokens hold a title and an abstract whole.
MAX_SEQ_LENGTH = 512


def load_model(model_name: str):
    from sentence_transformers import SentenceTransformer

    # Asking the Hub whether a cached model changed costs over a minute
    # through a slow proxy; go online only if it is not cached yet.
    try:
        model = SentenceTransformer(model_name, local_files_only=True)
    except OSError:
        model = SentenceTransformer(model_name)
    model.max_seq_length = MAX_SEQ_LENGTH
    return model


class Embedder:
    """The query side of the embedding model, loaded on first use."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None

    @property
    def available(self) -> bool:
        # sentence-transformers is the optional `search` extra; without it
        # only BM25 can run.
        return importlib.util.find_spec("sentence_transformers") is not None

    def __call__(self, phrases: list[str]) -> np.ndarray:
        if self._model is None:
            self._model = load_model(self.model_name)
        return self._model.encode(phrases, normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)
