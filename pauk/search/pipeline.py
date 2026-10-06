"""One question end to end: plan → entry points → subgraph → ranking → answer."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

from pauk.settings import Settings, settings

from . import templates
from .entities import Resolution, load_department_index, load_person_index
from .llm import LLMError, compose_answer, make_client, parse_question
from .plan import QueryPlan, fallback_plan
from .ranking import preset, rank, trim
from .reader import GraphReader
from .retrieval import Collection, Embedder, Hit, Mode

logger = logging.getLogger(__name__)


@dataclass
class AskOptions:
    retrieval_mode: Mode = "hybrid"
    ranking: str = "graph"  # "graph" or "authorship", see ranking.PRESETS
    llm_parse: bool = True
    llm_answer: bool = True
    top_k_publications: int = 100
    top_k_repositories: int = 30
    min_score: float = 0.0  # drop entry points below this relevance
    top_n: int = 10
    plan: QueryPlan | None = None  # a ready plan skips LLM №1 (manual runs, evaluation)


@dataclass
class _Timer:
    marks: dict[str, float] = field(default_factory=dict)
    start: float = field(default_factory=time.perf_counter)

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks[name] = round(now - self.start, 3)
        self.start = now


class Engine:
    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        data_dir = self.config.search_dir
        if not (data_dir / "manifest.json").exists():
            raise FileNotFoundError(f"no search index in {data_dir}; build it first: pauk search build-index")
        self.manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
        self.graph = GraphReader(self.config)
        self.publications = Collection.load(data_dir, "publications")
        self.repositories = Collection.load(data_dir, "repositories")
        self.pub_titles = {
            row["id"]: row.get("title")
            for row in json.loads((data_dir / "publications.json").read_text(encoding="utf-8"))
        }
        self.persons = load_person_index(data_dir)
        self.departments = load_department_index(data_dir)
        self.embedder = Embedder(self.manifest["embedding_model"])
        self.llm = make_client(self.config)

    def close(self) -> None:
        self.graph.close()

    def ask(self, question: str, options: AskOptions | None = None) -> dict[str, Any]:
        options = options or AskOptions()
        timer = _Timer()
        notes: list[str] = []
        trace: dict[str, Any] = {}

        if options.plan is not None:
            plan = options.plan
        elif options.llm_parse:
            plan, trace["parse"] = parse_question(self.llm, question)
            if "fallback" in trace["parse"]:
                notes.append("LLM не разобрала вопрос, выполнен поиск по теме всего вопроса.")
        else:
            plan = fallback_plan(question)
        timer.mark("parse")

        resolutions = [self._resolve(entity.kind, entity.name) for entity in plan.entities]
        anchor = next((r for r in resolutions if r.best), None)
        for resolution in resolutions:
            if not resolution.best:
                notes.append(f"Не найдено в графе: «{resolution.query}».")
            elif resolution.ambiguous:
                options_text = "; ".join(c.label for c in resolution.candidates[:5])
                notes.append(f"Имя «{resolution.query}» неоднозначно, выбран «{resolution.best.label}». "
                             f"Другие варианты: {options_text}.")
        timer.mark("entities")

        if plan.type in ("graph", "mixed") and anchor is None:
            return self._result(question, plan, options, resolutions, [], {}, {}, None, notes, trace, timer)

        counts: dict[str, Any] = {}
        hits: list[Hit] = []
        if plan.type == "graph":
            data, pub_rel, repo_rel, counts = self._profile(anchor, plan)
        else:
            if options.retrieval_mode != "bm25" and not self.embedder.available:
                notes.append("Модель эмбеддингов не установлена (pip extra `search`), поиск выполнен только BM25.")
                options = replace(options, retrieval_mode="bm25")
            allowed = self._anchor_publications(anchor) if plan.type == "mixed" else None
            hits = self.publications.search(options.retrieval_mode, plan.core, self.embedder,
                                            options.top_k_publications, allowed)
            repo_hits = [] if allowed is not None else self.repositories.search(
                options.retrieval_mode, plan.core, self.embedder, options.top_k_repositories)
            pub_rel = {h.id: h.score for h in hits if h.score >= options.min_score}
            repo_rel = {h.id: h.score for h in repo_hits if h.score >= options.min_score}
            timer.mark("retrieval")
            trace["repository_hits"] = [{"id": h.id, "score": round(h.score, 4)} for h in repo_hits[:10]]
            data = templates.topic_subgraph(self.graph, list(pub_rel), list(repo_rel))
        timer.mark("graph")

        ranking = preset(options.ranking)
        sections = trim(rank(data, pub_rel, repo_rel, plan, ranking), plan, options.top_n)
        if counts and ("counts" in plan.expected_values or plan.type == "graph"):
            sections["counts"] = counts
        timer.mark("ranking")

        answer = None
        if options.llm_answer:
            try:
                answer = compose_answer(self.llm, question, sections, notes)
            except LLMError as exc:
                notes.append(f"Текстовый ответ не получен: {exc}")
            timer.mark("answer")
        return self._result(question, plan, options, resolutions, hits, sections, counts, answer, notes, trace, timer)

    def _resolve(self, kind: str, name: str) -> Resolution:
        index = self.persons if kind == "person" else self.departments
        return index.resolve(name)

    def _anchor_publications(self, anchor: Resolution) -> set[str]:
        if anchor.kind == "person":
            return set(templates.person_publication_ids(self.graph, anchor.best.id))
        units = templates.department_units(self.graph, anchor.best.id)
        return set(templates.department_publication_ids(self.graph, units))

    def _profile(self, anchor: Resolution, plan: QueryPlan):
        if anchor.kind == "person":
            data = templates.person_profile(self.graph, anchor.best.id)
        else:
            data = templates.department_profile(self.graph, anchor.best.id)
        # No topic: every publication of the entity counts in full.
        pub_rel = {row["id"]: 1.0 for row in data["publications"]}
        repo_rel = {row["id"]: 1.0 for row in data["repositories"]}
        years = [row.get("year") for row in data["publications"]]
        in_range = [
            y for y in years
            if (plan.filter.year_from is None or (y and y >= plan.filter.year_from))
            and (plan.filter.year_to is None or (y and y <= plan.filter.year_to))
        ]
        counts = {
            "entity": anchor.best.label,
            "publications": len(in_range),
            "publications_total": len(years),
            "by_year": dict(sorted(Counter(y for y in in_range if y).items())),
            "repositories": len(data["repositories"]),
        }
        if anchor.kind == "person":
            counts["coauthors"] = len({row["person"] for row in data["authorship"]} - {anchor.best.id})
        else:
            counts["members"] = len(data.get("members") or [])
            counts["units"] = len(data.get("units") or [])
        return data, pub_rel, repo_rel, counts

    def _result(self, question, plan, options, resolutions, hits, sections, counts, answer, notes, trace, timer):
        return {
            "question": question,
            "plan": plan.model_dump(),
            "options": {
                "retrieval_mode": options.retrieval_mode, "ranking": options.ranking,
                "llm_parse": options.llm_parse, "llm_answer": options.llm_answer, "top_n": options.top_n,
            },
            "entities": [
                {"query": r.query, "kind": r.kind, "ambiguous": r.ambiguous,
                 "candidates": [c.__dict__ for c in r.candidates[:5]]}
                for r in resolutions
            ],
            "entry_points": [
                {"id": h.id, "title": self.pub_titles.get(h.id), "score": round(h.score, 4),
                 "dense": None if h.dense is None else round(h.dense, 4),
                 "bm25": None if h.bm25 is None else round(h.bm25, 3)}
                for h in hits[:20]
            ],
            "sections": sections,
            "answer": answer,
            "notes": notes,
            "trace": trace,
            "timings": timer.marks,
        }
