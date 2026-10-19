"""The embedding model, shared by indexing (documents) and retrieval (questions)."""

from __future__ import annotations

import importlib.util

import numpy as np

from .models import Model

# bge-m3 accepts 8192 tokens, but a long README at that length exhausts an
# 8 GB GPU; 512 tokens hold a title with an abstract, or a README head, whole.
MAX_SEQ_LENGTH = 512


def available() -> bool:
    # sentence-transformers (and torch under it) is the optional `rag` extra.
    return importlib.util.find_spec("sentence_transformers") is not None


class Embedder:
    """Loaded on first use: CLI commands that never embed should not pay for torch."""

    def __init__(self, model: Model, batch_size: int = 16) -> None:
        self.model = model
        self.model_name = model.name
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            # Asking the Hub whether a cached model changed costs over a
            # minute through a slow proxy; go online only if it is not cached.
            try:
                self._model = SentenceTransformer(self.model_name, local_files_only=True)
            except OSError:
                self._model = SentenceTransformer(self.model_name)
            self._model.max_seq_length = MAX_SEQ_LENGTH
        return self._model

    def unload(self) -> None:
        """Free the GPU: an 8 GB card holds about three of these models at once."""
        if self._model is None:
            return
        self._model = None
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    @property
    def dimensions(self) -> int:
        return int(self._load().get_sentence_embedding_dimension())

    def encode(self, texts: list[str], progress: bool = False, query: bool = False) -> np.ndarray:
        """Unit-length float32 vectors, so cosine similarity is a dot product.

        `query` picks the model's question prefix over its document one.
        """
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        prefix = self.model.query_prefix if query else self.model.document_prefix
        texts = [prefix + text for text in texts] if prefix else texts
        return self._load().encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                   convert_to_numpy=True, show_progress_bar=progress).astype(np.float32)
