"""Embedding models a profile can be embedded with, by short alias.

Each model keeps its own vector property and index per profile
(`rag_vec_full_qwen3`), so models sit side by side in Neo4j and are compared
on one pool instead of overwriting each other's vectors.
"""

from __future__ import annotations

from dataclasses import dataclass

# Instruction-tuned models are trained with the task stated before the
# query (documents go in bare); without it they lose noticeably, and the
# comparison would be unfair to them.
TASK = "Given a question about a research topic, retrieve scientific papers and code repositories on that topic"


@dataclass(frozen=True)
class Model:
    alias: str
    name: str
    query_prefix: str = ""
    document_prefix: str = ""
    # Below this raw score of the best match the model has found nothing on
    # the topic and `ask` answers empty. The scale is the vector index's,
    # (1 + cos) / 2, and differs between models; None never abstains.
    # Set by `pauk rag calibrate` (2026-10-15: 40 off-topic questions,
    # keywords as the query, 95th percentile), never from the validation set,
    # so the empty answers measured there are an honest estimate.
    min_score: float | None = None


MODELS: dict[str, Model] = {
    model.alias: model for model in (
        Model("bge", "BAAI/bge-m3", min_score=0.798),
        # The same XLM-R large backbone as bge-m3, retrained for retrieval.
        Model("arctic", "Snowflake/snowflake-arctic-embed-l-v2.0", query_prefix="query: ", min_score=0.696),
        Model("qwen3", "Qwen/Qwen3-Embedding-0.6B", query_prefix=f"Instruct: {TASK}\nQuery:", min_score=0.770),
    )
}


def get(key: str) -> Model:
    """A model by alias (`qwen3`) or by its Hugging Face name."""
    for model in MODELS.values():
        if key in (model.alias, model.name):
            return model
    raise ValueError(f"unknown embedding model {key!r}; known: {', '.join(MODELS)}")
