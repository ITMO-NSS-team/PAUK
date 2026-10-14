"""Retrieval-augmented answers over the PAUK graph.

The question is embedded as it is - no LLM rewrites it first - so this is
the plain baseline that smarter steps can later be measured against:

    rag = Rag(config)
    result = rag.ask("кто занимается федеративным обучением", profile="full", mode="hybrid")

`retrieve` finds publications and repositories (dense, BM25 or both),
`experts` turns them into people through AUTHORED / CONTRIBUTED_TO, and
`answer` lets the LLM word a reply that may cite only what was retrieved.
Retrieval and generation are separate steps so that metrics measure
retrieval, and the answer is judged on its own.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pauk.settings import Settings
from pauk.sources.llm import OpenRouterClient

from .embedder import Embedder
from .graph import Graph, escape_lucene
from .index import load_manifest
from .profiles import LABELS, PRESETS, Profile

Mode = Literal["dense", "bm25", "hybrid"]
MODES: tuple[str, ...] = ("dense", "bm25", "hybrid")
# hybrid = DENSE_WEIGHT · dense + (1 - DENSE_WEIGHT) · bm25, each min-max scaled
# per question first: cosine and BM25 live on different scales. Dense gets
# more weight because the question is Russian and most texts English, where
# BM25 can match only names and numbers.
DENSE_WEIGHT = 0.7
# Reciprocal rank fusion constant (Cormack et al., 2009): ranks, not scores,
# so no scaling is needed; the alternative fusion for `fusion="rrf"`.
RRF_K = 60

PERSONS_OF_PUBLICATIONS = """
UNWIND $ids AS id
MATCH (person:Person)-[:AUTHORED]->(pub:Publication {id: id})
RETURN pub.id AS item, person.id AS person
"""
PERSONS_OF_REPOSITORIES = """
UNWIND $ids AS id
MATCH (person:Person)-[:CONTRIBUTED_TO]->(repo:Repository {id: id})
RETURN repo.id AS item, person.id AS person
"""
PERSON_DETAILS = """
UNWIND $ids AS id
MATCH (p:Person {id: id})
RETURN p.id AS id, p.name_ru AS name_ru, p.name_en AS name_en, p.name_raw AS name_raw,
       p.name_variants AS name_variants, p.orcid AS orcid, p.openalex_id AS openalex_id,
       coalesce(p.is_itmo, false) AS is_itmo,
       [(p)-[:BELONGS_TO]->(d:Department) | coalesce(d.name_ru, d.name_en)] AS departments
"""
PUBLICATION_DETAILS = """
UNWIND $ids AS id
MATCH (p:Publication {id: id})
RETURN p.id AS id, p.title AS title, p.year AS year, p.journal AS journal, p.doi AS doi,
       p.openalex_url AS openalex_url, p.type AS type, p[$text] AS text,
       [(a:Person)-[r:AUTHORED]->(p) | {name: coalesce(a.name_ru, a.name_en, a.name_raw), position: r.position}]
         AS authors
"""
REPOSITORY_DETAILS = """
UNWIND $ids AS id
MATCH (r:Repository {id: id})
RETURN r.id AS id, r.name AS name, r.url AS url, r.description AS description, r.stars_num AS stars,
       r[$text] AS text,
       [(c:Person)-[:CONTRIBUTED_TO]->(r) | coalesce(c.name_ru, c.name_en, c.name_raw)] AS contributors
"""


@dataclass
class Hit:
    id: str
    label: str
    score: float
    dense: float | None = None
    bm25: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


def person_label(row: dict[str, Any]) -> str:
    variants = row.get("name_variants") or []
    return row.get("name_ru") or row.get("name_en") or row.get("name_raw") or (variants[0] if variants else row["id"])


def person_url(row: dict[str, Any]) -> str | None:
    if row.get("orcid"):
        return f"https://orcid.org/{row['orcid']}"
    if row.get("openalex_id"):
        return f"https://openalex.org/{row['openalex_id']}"
    return None


def publication_url(row: dict[str, Any]) -> str | None:
    doi = row.get("doi")
    if doi:
        return doi if doi.startswith("http") else f"https://doi.org/{doi}"
    return row.get("openalex_url")


def min_max(scored: list[tuple[str, float]]) -> dict[str, float]:
    if not scored:
        return {}
    values = [score for _, score in scored]
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return {doc_id: 1.0 for doc_id, _ in scored}
    return {doc_id: (score - low) / (high - low) for doc_id, score in scored}


def fuse(dense: list[tuple[str, float]], bm25: list[tuple[str, float]], fusion: str) -> dict[str, float]:
    if fusion == "rrf":
        scores: dict[str, float] = defaultdict(float)
        for ranked in (dense, bm25):
            for rank, (doc_id, _) in enumerate(ranked, 1):
                scores[doc_id] += 1 / (RRF_K + rank)
        return dict(scores)
    if fusion != "minmax":
        raise ValueError("fusion: minmax or rrf")
    d, b = min_max(dense), min_max(bm25)
    return {doc_id: DENSE_WEIGHT * d.get(doc_id, 0.0) + (1 - DENSE_WEIGHT) * b.get(doc_id, 0.0)
            for doc_id in d.keys() | b.keys()}


class Rag:
    def __init__(self, config: Settings, *, graph: Graph | None = None, embedder: Embedder | None = None,
                 llm: OpenRouterClient | None = None) -> None:
        self.config = config
        self.graph = graph or Graph(config)
        self.embedder = embedder or Embedder(config.rag_embedding_model)
        self._llm = llm

    def close(self) -> None:
        self.graph.close()

    @property
    def llm(self) -> OpenRouterClient:
        if self._llm is None:
            self._llm = OpenRouterClient(self.config.request_timeout, self.config.openrouter_api_key,
                                         self.config.llm_model, self.config.openrouter_proxy_url)
        return self._llm

    def profile(self, name: str) -> Profile:
        manifest = load_manifest(self.config, name)
        if manifest is None:
            raise ValueError(f"profile {name!r} is not built; run `pauk rag index --profile {name}`")
        if manifest["embedding_model"] != self.config.rag_embedding_model:
            # Question and documents must be embedded by the same model.
            raise ValueError(f"profile {name!r} was built with {manifest['embedding_model']}, "
                             f"not {self.config.rag_embedding_model}")
        spec = manifest["profile"]
        return PRESETS.get(name) if PRESETS.get(name) and PRESETS[name].to_dict() == spec else Profile(
            spec["name"], tuple(spec["publication"]), tuple(spec["repository"]), spec["skip_non_research"])

    # --- retrieval ----------------------------------------------------------------

    def translate(self, question: str) -> str:
        """English search keywords for the question, cached on disk per model.

        Only BM25 uses them: the embedding model is multilingual and reads
        the question as written, while full-text search needs the words the
        mostly English texts use.
        """
        path = self.config.data_dir / "rag" / "translations.json"
        cache = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        key = f"{self.llm.model}\0{question}"
        if key not in cache:
            raw = self.llm.chat_json(question, system_prompt=TRANSLATE_SYSTEM, max_tokens=TRANSLATE_MAX_TOKENS)
            query = raw.get("query") if isinstance(raw, dict) else None
            if not isinstance(query, str) or not query.strip():
                raise RuntimeError(f"no translation: {self.llm.last_error or raw!r}")
            cache[key] = query.strip()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        return cache[key]

    def retrieve(self, question: str, *, profile: str = "full", mode: Mode = "hybrid", k: int = 50,
                 fusion: str = "minmax", bm25_query: str | None = None,
                 labels: tuple[str, ...] = LABELS) -> dict[str, list[Hit]]:
        """Top `k` publications and repositories for the question, best first.

        `bm25_query` replaces the question for full-text search only (the
        translation from `translate`); the caller obtains it, so a failed
        translation is handled once, where the caller can report it.
        """
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        spec = self.profile(profile)
        vector = self.embedder.encode([question])[0].tolist() if mode != "bm25" else None
        terms = escape_lucene(bm25_query or question)
        out: dict[str, list[Hit]] = {}
        for label in labels:
            if label not in spec.labels():
                continue
            # Each side looks deeper than k, so that fusion can lift an item
            # one side ranked just below the cut.
            depth = k * 2 if mode == "hybrid" else k
            dense = self.graph.vector_search(label, spec.vector_index(label), vector, depth) if vector else []
            bm25 = self.graph.text_search(spec.text_index(label), terms, depth) if mode != "dense" else []
            if mode == "dense":
                scores = dict(dense)
            elif mode == "bm25":
                scores = dict(bm25)
            else:
                scores = fuse(dense, bm25, fusion)
            raw_dense, raw_bm25 = dict(dense), dict(bm25)
            ranked = sorted(scores.items(), key=lambda item: -item[1])[:k]
            out[label] = [Hit(doc_id, label, score, raw_dense.get(doc_id), raw_bm25.get(doc_id))
                          for doc_id, score in ranked]
        return out

    def experts(self, hits: dict[str, list[Hit]], *, top_n: int = 10, only_itmo: bool = True,
                power: float = 2.0, repository_weight: float = 0.5) -> list[dict[str, Any]]:
        """People ranked by the relevance of what they wrote or coded.

        A person's score is the sum over their retrieved items of
        relevance ** power (Balog et al.'s document model). The power makes
        one paper squarely on the topic outweigh several loosely related
        ones; a repository counts `repository_weight` of a paper, since
        committing to code is weaker evidence of expertise than authorship.
        Scores are min-max scaled per label first, so a dense cosine and a
        BM25 score of the same rank weigh the same.
        """
        relevance = {label: min_max([(h.id, h.score) for h in items]) for label, items in hits.items()}
        evidence: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"Publication": [], "Repository": []})
        score: dict[str, float] = defaultdict(float)
        for label, query, weight in (("Publication", PERSONS_OF_PUBLICATIONS, 1.0),
                                     ("Repository", PERSONS_OF_REPOSITORIES, repository_weight)):
            ids = list(relevance.get(label, {}))
            if not ids:
                continue
            for row in self.graph.read(query, ids=ids):
                score[row["person"]] += weight * relevance[label][row["item"]] ** power
                evidence[row["person"]][label].append(row["item"])
        details = {row["id"]: row for row in self.graph.read(PERSON_DETAILS, ids=list(score))}
        order = {label: {h.id: i for i, h in enumerate(items)} for label, items in hits.items()}
        people = []
        for person_id, value in sorted(score.items(), key=lambda item: -item[1]):
            row = details.get(person_id)
            if row is None or (only_itmo and not row["is_itmo"]):
                continue
            people.append({
                "id": person_id, "label": person_label(row), "url": person_url(row), "is_itmo": row["is_itmo"],
                "departments": row["departments"], "score": round(value, 4),
                "publications": sorted(evidence[person_id]["Publication"], key=order.get("Publication", {}).get),
                "repositories": sorted(evidence[person_id]["Repository"], key=order.get("Repository", {}).get),
            })
            if len(people) == top_n:
                break
        return people

    def describe(self, hits: dict[str, list[Hit]], profile: str) -> None:
        """Titles, years, authors and the indexed text, for the answer and for judges."""
        spec = self.profile(profile)
        for label, items in hits.items():
            if not items:
                continue
            if label == "Publication":
                rows = self.graph.read(PUBLICATION_DETAILS, ids=[h.id for h in items], text=spec.text_property)
            else:
                rows = self.graph.read(REPOSITORY_DETAILS, ids=[h.id for h in items], text=spec.text_property)
            by_id = {row["id"]: row for row in rows}
            for hit in items:
                row = by_id.get(hit.id, {})
                if label == "Publication":
                    authors = [a["name"] for a in sorted(row.get("authors") or [], key=lambda a: a["position"] or 0)]
                    hit.details = {"title": row.get("title"), "year": row.get("year"), "journal": row.get("journal"),
                                   "type": row.get("type"), "url": publication_url(row), "authors": authors,
                                   "text": row.get("text") or ""}
                else:
                    hit.details = {"title": row.get("name"), "url": row.get("url"), "stars": row.get("stars"),
                                   "description": row.get("description"), "contributors": row.get("contributors") or [],
                                   "text": row.get("text") or ""}

    # --- generation ---------------------------------------------------------------

    def answer(self, question: str, hits: dict[str, list[Hit]], people: list[dict[str, Any]], *,
               sources: int = 8) -> dict[str, Any]:
        """The LLM's reply, citing only the numbered sources it was given."""
        numbered, context = [], []
        for label in LABELS:
            for hit in hits.get(label, [])[:sources]:
                numbered.append(hit)
                kind = "публикация" if label == "Publication" else "репозиторий"
                context.append({"n": len(numbered), "kind": kind, "title": hit.details.get("title"),
                                "year": hit.details.get("year"), "url": hit.details.get("url"),
                                "authors": (hit.details.get("authors") or hit.details.get("contributors") or [])[:8],
                                "text": (hit.details.get("text") or "")[:1200]})
        experts = [{"name": p["label"], "departments": p["departments"],
                    "sources": [n for n, h in enumerate(numbered, 1) if h.id in p["publications"] + p["repositories"]]}
                   for p in people]
        payload = {"question": question, "sources": context, "experts": experts}
        raw = self.llm.chat_json(json.dumps(payload, ensure_ascii=False), system_prompt=ANSWER_SYSTEM,
                                 max_tokens=ANSWER_MAX_TOKENS)
        text = raw.get("answer") if isinstance(raw, dict) else None
        if not isinstance(text, str):
            return {"text": None, "error": self.llm.last_error or "no answer in the model's response", "cited": []}
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 1 <= int(n) <= len(numbered)})
        # A citation past the numbered sources is the model's invention: drop it.
        text = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(numbered) else "", text)
        return {"text": text, "cited": [numbered[n - 1].id for n in cited],
                "sources": [{"n": n, "id": h.id, "label": h.label, "title": h.details.get("title"),
                             "url": h.details.get("url")} for n, h in enumerate(numbered, 1)]}

    def ask(self, question: str, *, profile: str = "full", mode: Mode = "hybrid", k: int = 50,
            top_n: int = 10, fusion: str = "minmax", translate: bool = True, only_itmo: bool = True,
            with_answer: bool = True) -> dict:
        """One answer; `translate` defaults on, since hybrid:en won the 2026-10-07 validation.

        A failed translation (no API key, network) degrades to BM25 on the
        question as written instead of failing the answer, and says so.
        """
        timings: dict[str, float] = {}
        notes: list[str] = []
        start = time.perf_counter()
        bm25_query = None
        if translate and mode != "dense":
            try:
                bm25_query = self.translate(question)
            except Exception as exc:  # the answer is still worth giving without it
                notes.append(f"Перевод вопроса для BM25 не получен ({exc}); поиск по словам вопроса как есть.")
                translate = False
        hits = self.retrieve(question, profile=profile, mode=mode, k=k, fusion=fusion, bm25_query=bm25_query)
        timings["retrieve"] = round(time.perf_counter() - start, 3)
        people = self.experts(hits, top_n=top_n, only_itmo=only_itmo)
        timings["experts"] = round(time.perf_counter() - start - timings["retrieve"], 3)
        shown = {label: items[:top_n] for label, items in hits.items()}
        self.describe(shown, profile)
        answer = None
        if with_answer:
            mark = time.perf_counter()
            answer = self.answer(question, shown, people)
            timings["answer"] = round(time.perf_counter() - mark, 3)
        return {
            "question": question,
            "options": {"profile": profile, "mode": mode, "k": k, "top_n": top_n, "fusion": fusion,
                        "translate": translate, "only_itmo": only_itmo},
            "persons": people,
            "publications": [asdict(h) for h in shown.get("Publication", [])],
            "repositories": [asdict(h) for h in shown.get("Repository", [])],
            "answer": answer,
            "notes": notes,
            "timings": timings,
        }


ANSWER_SYSTEM = """Ты отвечаешь на вопрос о науке в Университете ИТМО по найденным источникам.
Тебе даны источники с номерами (публикации и репозитории) и список экспертов с номерами их источников.
Правила:
- Опирайся ТОЛЬКО на эти источники. Не добавляй людей, работ и фактов, которых в них нет.
- После каждого утверждения ставь номер источника в квадратных скобках: [1], [2][5].
- Если источники не отвечают на вопрос или отвечают частично, прямо скажи об этом, а не подгоняй ответ.
- Пиши по-русски, кратко: до 200 слов, при необходимости короткий список.
Верни JSON-объект {"answer": "<текст в Markdown>"}."""
# A cap keeps a looping model from running for minutes; 900 tokens hold 200 words with citations.
ANSWER_MAX_TOKENS = 900

TRANSLATE_SYSTEM = """Turn the user's question about research into English search keywords for a full-text
search over paper titles, abstracts and repository READMEs. Translate the topic into the terms papers use.
Keep names of models, methods, datasets, software and people exactly as written. Drop question words and
filler ("who", "is there", "where", "in ITMO"). Return a JSON object {"query": "<keywords>"}."""
TRANSLATE_MAX_TOKENS = 120
