"""Optional semantic-neighbour backend for stage 1.

Lexical blocking never surfaces a Russian name next to its English one, or an
acronym next to its expansion — those pairs share no surface form. A
multilingual sentence embedder (LaBSE is trained on RU<->EN bitext) closes
that gap: `semantic_pairs` returns id pairs whose names are close in embedding
space, and they join the candidate set.

The backend is optional and off by default: with no `--embedder` the pipeline
runs lexical-only — the LLM still bridges RU<->EN on the pairs that do get
blocked, there are just fewer of them. To enable it, install
sentence-transformers (kept out of the project dependencies — it pulls in
torch) and pass `pauk dedup departments --embedder labse`.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# Pinned so a re-run reproduces the same neighbour set.
_MODELS = {
    "labse": "sentence-transformers/LaBSE",
    "minilm": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
}
_NEIGHBOUR_THRESHOLD = 0.70


class Embedder(Protocol):
    def semantic_pairs(self, texts_by_id: dict[str, list[str]]) -> set[tuple[str, str]]: ...


class _SentenceTransformerEmbedder:
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self._model = SentenceTransformer(model_name)
        self._model_name = model_name

    def semantic_pairs(self, texts_by_id: dict[str, list[str]]) -> set[tuple[str, str]]:
        import numpy as np  # noqa: PLC0415

        ids: list[str] = []
        rows: list[str] = []
        for dept_id, names in texts_by_id.items():
            for name in names:
                ids.append(dept_id)
                rows.append(name)
        if not rows:
            return set()
        vectors = self._model.encode(rows, normalize_embeddings=True, show_progress_bar=False)
        sims = np.asarray(vectors) @ np.asarray(vectors).T
        pairs: set[tuple[str, str]] = set()
        for i in range(len(rows)):
            for j in np.where(sims[i] >= _NEIGHBOUR_THRESHOLD)[0]:
                a, b = ids[i], ids[int(j)]
                if a != b:
                    pairs.add(tuple(sorted((a, b))))  # type: ignore[arg-type]
        logger.info("dept dedup: %s added %d semantic candidate pair(s)", self._model_name, len(pairs))
        return pairs


def load_embedder(name: str) -> Embedder | None:
    key = (name or "").strip().lower()
    if not key or key == "none":
        return None
    model_name = _MODELS.get(key, key if "/" in key else None)
    if model_name is None:
        logger.warning("dept dedup: unknown embedder %r — running lexical-only", name)
        return None
    try:
        return _SentenceTransformerEmbedder(model_name)
    except ImportError:
        logger.warning(
            "dept dedup: --embedder %s requested but sentence-transformers is not installed "
            "(`pip install sentence-transformers`) — running lexical-only", name)
        return None
