"""Dictionary-free deduplication of Department nodes in the graph.

`pauk dedup departments` — a funnel that replaces the hand-maintained
`aliases` catalog: normalize (stage 0) -> block into candidate pairs
(stage 1) -> deterministic signals + banding (stage 2) -> LLM adjudication of
the ambiguous band (stage 4) -> union-find grouping with a conflict guard ->
fold each group into one canonical node.

See docs/architecture/graph-dept-dedup.md.
"""

from .pipeline import run_department_dedup

__all__ = ["run_department_dedup"]
