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


MODELS: dict[str, Model] = {
    model.alias: model for model in (
        Model("bge", "BAAI/bge-m3"),
        # The same XLM-R large backbone as bge-m3, retrained for retrieval.
        Model("arctic", "Snowflake/snowflake-arctic-embed-l-v2.0", query_prefix="query: "),
        Model("qwen3", "Qwen/Qwen3-Embedding-0.6B", query_prefix=f"Instruct: {TASK}\nQuery:"),
    )
}


def get(key: str) -> Model:
    """A model by alias (`qwen3`) or by its Hugging Face name."""
    for model in MODELS.values():
        if key in (model.alias, model.name):
            return model
    raise ValueError(f"unknown embedding model {key!r}; known: {', '.join(MODELS)}")
