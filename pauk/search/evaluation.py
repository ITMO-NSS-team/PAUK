"""Comparing search variants by pooling, the way TREC does it.

Nobody can judge all 13 thousand publications for every question. Instead
each variant answers the question, the top `k` of every variant are merged
into one pool, and only the pool is judged (0 - not relevant, 1 - partly,
2 - relevant). What no variant returned counts as not relevant. That makes
precision-type metrics (P@k, nDCG@k, MRR) comparable across the variants;
recall is not measured, since the full set of relevant items is unknown.

The variants share the plan LLM №1 made for the batch run, so the
comparison costs no tokens and measures retrieval and ranking only.
Files live next to the run: `pool.json` (what each variant returned and
what the judge sees), `judgments.json` (the grades), `metrics.csv`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .pipeline import AskOptions, Engine
from .plan import SECTIONS, QueryPlan

K = 10
KINDS = ("persons", "publications")
GRADES = (0, 1, 2)
# Retrieval alone (authorship ranking: a person is the sum of their papers'
# relevance), then the graph's extra structure on top of the two strongest.
VARIANTS: dict[str, dict[str, str]] = {
    "bm25": {"retrieval_mode": "bm25", "ranking": "authorship"},
    "dense": {"retrieval_mode": "dense", "ranking": "authorship"},
    "hybrid": {"retrieval_mode": "hybrid", "ranking": "authorship"},
    "dense+graph": {"retrieval_mode": "dense", "ranking": "graph"},
    "hybrid+graph": {"retrieval_mode": "hybrid", "ranking": "graph"},
}
ABSTRACT_CHARS = 600


def _plan_for_pool(result: dict[str, Any]) -> QueryPlan | None:
    """The batch run's plan, widened so every variant returns every judged section."""
    plan = result.get("plan")
    if not plan or not plan.get("core"):
        return None  # a pure profile or count question: retrieval plays no part
    widened = QueryPlan.model_validate(plan)
    return widened.model_copy(update={"expected_values": list(SECTIONS[:4]), "fields": []})


def _shuffled(question: str, ids: list[str]) -> list[str]:
    """A fixed order that hides which variant ranked an item where."""
    return sorted(ids, key=lambda i: hashlib.sha1(f"{question}\0{i}".encode()).hexdigest())


def build_pool(run_dir: Path, engine: Engine, k: int = K) -> dict[str, Any]:
    results = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    texts = {row["id"]: row.get("text") or "" for row in json.loads(
        (engine.config.search_dir / "publications.json").read_text(encoding="utf-8"))}
    questions = []
    for result in results:
        entry: dict[str, Any] = {"question": result["question"], "meta": result.get("meta") or {}}
        plan = None if "error" in result else _plan_for_pool(result)
        if plan is None:
            entry["skipped"] = "нет темы для поиска: вопрос про профиль или подсчёт"
            questions.append(entry)
            continue
        rankings: dict[str, dict[str, list[str]]] = {}
        items: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in KINDS}
        for name, variant in VARIANTS.items():
            answer = engine.ask(result["question"], AskOptions(
                plan=plan, llm_parse=False, llm_answer=False, top_n=k, **variant))
            rankings[name] = {}
            for kind in KINDS:
                rows = (answer["sections"].get(kind) or [])[:k]
                rankings[name][kind] = [row["id"] for row in rows]
                for row in rows:
                    _remember(items[kind], kind, row, texts)
        entry.update(
            rankings=rankings,
            items=items,
            order={kind: _shuffled(result["question"], list(items[kind])) for kind in KINDS},
        )
        questions.append(entry)
    pool = {"k": k, "variants": VARIANTS, "kinds": list(KINDS), "questions": questions}
    (run_dir / "pool.json").write_text(json.dumps(pool, ensure_ascii=False, indent=1), encoding="utf-8")
    return pool


def _remember(known: dict[str, dict[str, Any]], kind: str, row: dict[str, Any], texts: dict[str, str]) -> None:
    """What the judge needs to decide, merged over every variant that returned the item."""
    item = known.setdefault(row["id"], {"label": row.get("label"), "url": row.get("url")})
    if kind == "persons":
        item["departments"] = row.get("departments") or []
        item["is_itmo"] = row.get("is_itmo")
        seen = item.setdefault("publications", [])
        for title in row.get("top_publications") or []:
            if title and title not in seen:
                seen.append(title)
    else:
        title = row.get("title") or ""
        abstract = texts.get(row["id"], "").removeprefix(title).strip()
        item.update(year=row.get("year"), journal=row.get("journal"), authors=row.get("authors") or [],
                    abstract=abstract[:ABSTRACT_CHARS] + ("…" if len(abstract) > ABSTRACT_CHARS else ""))


# --- judgments -------------------------------------------------------------------

def load_pool(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "pool.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _judgments_path(run_dir: Path, judge: str | None) -> Path:
    """`judgments.json` for the person reviewing; `judgments_<judge>.json` for anyone else.

    A second judge (another person, or a model) never writes into the main
    file, so their grades can be compared with it instead of silently mixed in.
    """
    if judge is None:
        return run_dir / "judgments.json"
    if not re.fullmatch(r"[a-z0-9_]{1,32}", judge):
        raise ValueError("judge name: lowercase letters, digits and _")
    return run_dir / f"judgments_{judge}.json"


def load_judgments(run_dir: Path, judge: str | None = None) -> dict[str, dict[str, int]]:
    path = _judgments_path(run_dir, judge)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_judgment(run_dir: Path, question: str, kind: str, item_id: str, grade: int | None,
                  judge: str | None = None) -> None:
    """Set or (grade None) clear one grade. Keys are stable, so a rebuilt pool keeps them."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if grade is not None and grade not in GRADES:
        raise ValueError(f"grade must be one of {GRADES}")
    pool = load_pool(run_dir)
    entry = next((q for q in pool["questions"] if q["question"] == question), None)
    if entry is None or item_id not in (entry.get("items") or {}).get(kind, {}):
        raise KeyError(f"{kind} {item_id} is not in the pool of this question")
    judgments = load_judgments(run_dir, judge)
    grades = judgments.setdefault(question, {})
    if grade is None:
        grades.pop(f"{kind}:{item_id}", None)
    else:
        grades[f"{kind}:{item_id}"] = grade
    path = _judgments_path(run_dir, judge)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(judgments, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --- metrics -----------------------------------------------------------------------

def _dcg(gains: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(rank + 2) for rank, g in enumerate(gains))


def score_ranking(ranked: list[str], grades: dict[str, int], k: int) -> dict[str, float]:
    """P@5, P@k, nDCG@k and MRR of one ranked list; unjudged items count as not relevant."""
    top = ranked[:k]
    gains = [grades.get(item, 0) for item in top]
    relevant = [g >= 1 for g in gains]
    ideal = _dcg(sorted(grades.values(), reverse=True)[:k])
    first = next((rank for rank, hit in enumerate(relevant, 1) if hit), None)
    return {
        "P@5": sum(relevant[:5]) / 5,
        f"P@{k}": sum(relevant) / k,
        f"nDCG@{k}": _dcg(gains) / ideal if ideal else 0.0,
        "MRR": 1 / first if first else 0.0,
        f"judged@{k}": sum(item in grades for item in top) / k,
    }


def compute_metrics(run_dir: Path, judge: str | None = None) -> list[dict[str, Any]]:
    pool = load_pool(run_dir)
    judgments = load_judgments(run_dir, judge)
    k = pool["k"]
    rows = []
    for kind in pool["kinds"]:
        for variant in pool["variants"]:
            per_question = []
            for entry in pool["questions"]:
                if entry.get("skipped"):
                    continue
                prefix = f"{kind}:"
                grades = {key[len(prefix):]: g for key, g in judgments.get(entry["question"], {}).items()
                          if key.startswith(prefix)}
                if not grades:
                    continue  # an unjudged question would score 0 for everyone and say nothing
                per_question.append(score_ranking(entry["rankings"][variant][kind], grades, k))
            if not per_question:
                continue
            row: dict[str, Any] = {"kind": kind, "variant": variant, "questions": len(per_question)}
            for metric in per_question[0]:
                row[metric] = round(sum(q[metric] for q in per_question) / len(per_question), 3)
            rows.append(row)
    if rows:
        name = "metrics.csv" if judge is None else f"metrics_{judge}.csv"
        with (run_dir / name).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return rows


def agreement(run_dir: Path, judge: str, other: str | None = None) -> list[dict[str, Any]]:
    """How far two judges agree on the items both graded: exact share and Cohen's kappa.

    Kappa discounts the agreement two judges would reach by chance given how
    often each uses every grade; the weighted kappa also counts 1-vs-2 as a
    smaller disagreement than 0-vs-2.
    """
    a, b = load_judgments(run_dir, other), load_judgments(run_dir, judge)
    rows = []
    for kind in KINDS:
        pairs = [(grades[key], b[question][key])
                 for question, grades in a.items() for key in grades
                 if key.startswith(f"{kind}:") and key in b.get(question, {})]
        if pairs:
            rows.append({"kind": kind, "items": len(pairs),
                         "exact": round(sum(x == y for x, y in pairs) / len(pairs), 3),
                         "kappa": round(_kappa(pairs, weighted=False), 3),
                         "weighted_kappa": round(_kappa(pairs, weighted=True), 3)})
    return rows


def _kappa(pairs: list[tuple[int, int]], weighted: bool) -> float:
    n = len(pairs)
    span = max(GRADES) - min(GRADES)

    def disagreement(x: int, y: int) -> float:
        return abs(x - y) / span if weighted else float(x != y)

    observed = sum(disagreement(x, y) for x, y in pairs) / n
    left = Counter(x for x, _ in pairs)
    right = Counter(y for _, y in pairs)
    expected = sum(left[x] * right[y] * disagreement(x, y) for x in GRADES for y in GRADES) / n ** 2
    return 1.0 if expected == 0 else 1 - observed / expected


def progress(pool: dict[str, Any], judgments: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Judged / total pool items per question, for the review page."""
    out = {}
    for entry in pool["questions"]:
        if entry.get("skipped"):
            continue
        keys = {f"{kind}:{item}" for kind in pool["kinds"] for item in entry["items"][kind]}
        judged = keys & set(judgments.get(entry["question"], {}))  # grades of items a rebuild dropped don't count
        out[entry["question"]] = {"judged": len(judged), "total": len(keys)}
    return out
