"""Metrics of a text-to-Cypher baseline run (`t2c_run`) against the labelled bank.

Every question gets a type for the summary - no_answer when it has no answer
(by the gold for q…t1 and q…t2, by the bank's `type` for q…t3, which has no
gold), else graph, mixed or rag by its id group - and:

- an error class, the first failure in the pipeline's order: 1 no usable plan,
  2 wrong question type (the plan's against the id group), 3 an entity not
  found or resolved to another id than gold, 4 RAG targets other than the
  bank's, 5 Cypher rejected, 6 timeout or another Neo4j failure; or none.
  Answer quality is not an error: it is the score;
- a score in 0..1:
    graph     list F1 by id, scalar exact within 10%, table F1 over rows;
    rag       the mean of nDCG@50 and F1@50 of the best search variant over
              the question's target kinds, one per judge;
    mixed     the mean of EM, EM within 10%, nDCG@50 and F1@50 - EM terms are
              F1 for a list or a table - the RAG half judged by Luna;
    no_answer 1 when the system answered empty (an empty Cypher result, or
              the rag search abstaining under the model's min_score), 0 when
              it answered or broke;
  a question that failed scores what its (missing) answer is worth;
- a strict pass, the "perfect" answers: graph EM, rag grade-2 recall@50 >= 0.9
  and grade-0 share <= 0.1 for every target kind, mixed that and a Cypher
  part >= 0.9, no_answer its score.

The error table (types x classes) goes to errors.csv as well. The colleague's
metrics of every search variant are written next to each pool
(`metrics_<judge>.csv`), with people graded from their works as he does.

Usage:
    uv run python -m pauk.search.t2c_metrics --run baseline-1
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
from pathlib import Path
from typing import Any

import numpy as np

from pauk.rag import evaluation as rag_eval
from pauk.rag.models import get as get_model

from .t2c_run import GOLD, POOL_JUDGES, QUESTIONS, RUNS_DIR, group, read_bank

BEST_VARIANT = "full@qwen3:hybrid:en+kw"
K = 50
CUTOFFS = (10, K)
TOLERANCE = 0.10
STRICT_RECALL = 0.9
STRICT_ZERO_SHARE = 0.1
STRICT_QUERY = 0.9
JUDGES = ("luna", "qwen50")
TYPES = ("graph", "mixed", "rag", "no_answer")
ROUTE = {"1": "graph", "2": "mixed", "3": "rag"}
RUN_ERRORS = (1, 3, 5, 6)
ERROR_NAMES = {1: "invalid plan", 2: "wrong type", 3: "wrong entity", 4: "wrong rag params",
               5: "cypher rejected", 6: "neo4j failure"}


# --- answers against gold ------------------------------------------------------------

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def close(a: Any, b: Any, tolerance: float = TOLERANCE) -> bool:
    """Numbers within `tolerance` of the gold `b`; anything else equal as text."""
    x, y = _number(a), _number(b)
    if x is not None and y is not None:
        return x == y if y == 0 else abs(x - y) <= tolerance * abs(y)
    return a is not None and b is not None and str(a) == str(b)


def _f1(matched: int, found: int, expected: int) -> float:
    if found == 0 and expected == 0:
        return 1.0
    return 2 * matched / (found + expected) if found + expected else 0.0


def list_f1(found: list[Any], expected: list[Any]) -> float:
    found_set, expected_set = {str(x) for x in found}, {str(x) for x in expected}
    return _f1(len(found_set & expected_set), len(found_set), len(expected_set))


def table_f1(found: list[list[Any]], expected: list[list[Any]], tolerance: float = TOLERANCE) -> float:
    """F1 over rows matched one to one; numbers within `tolerance`.

    The model names and orders its columns itself, so a table of the gold's
    width is tried in every column order and scored by the best one.
    """
    if not found or not expected:
        return _f1(0, len(found), len(expected))
    width = len(expected[0])
    orders = itertools.permutations(range(width)) if all(len(row) == width for row in found) else [None]
    best = 0
    for order in orders:
        rows = [[row[i] for i in order] for row in found] if order else found
        free = list(range(len(rows)))
        matched = 0
        for gold_row in expected:
            hit = next((i for i in free if len(rows[i]) == len(gold_row)
                        and all(close(a, b, tolerance) for a, b in zip(rows[i], gold_row, strict=True))), None)
            if hit is not None:
                free.remove(hit)
                matched += 1
        best = max(best, matched)
    return _f1(best, len(found), len(expected))


def compare(answer: dict[str, Any] | None, gold: dict[str, Any]) -> dict[str, float]:
    """EM, EM within the tolerance and F1 of a system answer, in the gold's answer type."""
    kind = gold["type"]
    if answer is None:
        return {"em": 0.0, "em_tol": 0.0, "f1": 0.0}
    if kind == "scalar":
        value = answer.get("value") if answer.get("type") == "scalar" else None
        exact = float(close(value, gold["value"], 0.0))
        tolerant = float(close(value, gold["value"]))
        return {"em": exact, "em_tol": tolerant, "f1": tolerant}
    if kind == "table":
        f1 = table_f1(answer.get("rows") or [], gold["rows"]) if answer.get("type") == "table" else 0.0
        exact = table_f1(answer.get("rows") or [], gold["rows"], 0.0) if answer.get("type") == "table" else 0.0
        return {"em": float(exact == 1.0), "em_tol": float(f1 == 1.0), "f1": f1}
    f1 = list_f1(answer.get("value") or [], gold["value"]) if answer.get("type") == "list" else 0.0
    return {"em": float(f1 == 1.0), "em_tol": float(f1 == 1.0), "f1": f1}


def query_score(scores: dict[str, float], answer_type: str) -> float:
    """The Cypher part as one number: tolerant EM for a scalar, F1 otherwise."""
    return scores["em_tol"] if answer_type == "scalar" else scores["f1"]


def is_empty(answer: dict[str, Any] | None) -> bool:
    """An empty Cypher result; a count of 0 is the gold's no_answer too."""
    if answer is None:
        return False
    if answer["type"] == "scalar":
        return answer.get("value") in (None, 0)
    if answer["type"] == "table":
        return not answer.get("rows")
    return not answer.get("value")


# --- the RAG part -------------------------------------------------------------------------

def rag_part(entry: dict[str, Any] | None, judgments: dict[str, int], kinds: list[str]) -> dict[str, Any]:
    """nDCG@50, F1@50 and the strict check of the best variant, over the question's target kinds."""
    if entry is None or not kinds:
        return {"nDCG": 0.0, "F1": 0.0, "strict": False, "kinds": {}}
    ranking = entry["rankings"].get(BEST_VARIANT, {})
    per_kind = {}
    for kind in kinds:
        prefix = f"{kind}:"
        grades = {key[len(prefix):]: g for key, g in judgments.items() if key.startswith(prefix) and g is not None}
        ranked = ranking.get(kind) or []
        metrics = rag_eval.score_ranking(ranked, grades, CUTOFFS)
        precision, recall = metrics[f"P@{K}"], metrics[f"R@{K}"]
        top = [grades.get(item) for item in ranked[:K]]
        twos = sum(g == 2 for g in grades.values())
        # Recall of the answers proper over what a top-50 can hold; vacuous without any.
        recall_2 = sum(g == 2 for g in top) / min(K, twos) if twos else 1.0
        # Unjudged items are no grade 0; the share is of what was actually returned.
        zero_share = sum(g == 0 for g in top) / len(top) if top else 0.0
        per_kind[kind] = {**metrics, f"F1@{K}": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                          "R2": recall_2, "zero_share": zero_share,
                          "strict": recall_2 >= STRICT_RECALL and zero_share <= STRICT_ZERO_SHARE}
    return {"nDCG": float(np.mean([m[f"nDCG@{K}"] for m in per_kind.values()])),
            "F1": float(np.mean([m[f"F1@{K}"] for m in per_kind.values()])),
            "strict": all(m["strict"] for m in per_kind.values()), "kinds": per_kind}


def abstains(entry: dict[str, Any] | None, kinds: list[str]) -> bool:
    """The rag search answers empty: its best match is under the model's threshold (as `Rag.ask` does)."""
    if entry is None:
        return False
    label = "Repository" if kinds == ["repositories"] else "Publication"
    top = entry["rankings"].get(BEST_VARIANT, {}).get("top_dense", {}).get(label)
    threshold = get_model(rag_eval.parse_variant(BEST_VARIANT)["model"]).min_score
    return top is None or (threshold is not None and top < threshold)


# --- errors ------------------------------------------------------------------------------

def error_class(row: dict[str, Any], meta: dict[str, str], gold: dict[str, Any] | None) -> int | None:
    """The first failure in the pipeline's order, or None."""
    found = set()
    if row["error_class"] in RUN_ERRORS:
        found.add(row["error_class"])
    plan = row["plan"]
    if plan:
        if plan["type"] != ROUTE[group(meta["id"])]:
            found.add(2)
        if gold:
            resolved = {e["id"] for e in row["entities"]}
            if any(e.get("id") not in resolved for e in gold.get("entities", [])):
                found.add(3)
        expected = {t.strip() for t in meta.get("targets", "").split(",") if t.strip()}
        if plan["type"] in ("rag", "mixed") and expected and set(plan["rag"]["targets"]) != expected:
            found.add(4)
    return min(found) if found else None


# --- one question --------------------------------------------------------------------------

def question_type(meta: dict[str, str], gold: dict[str, Any] | None) -> str:
    """no_answer or the id group's type. Whether a graph or mixed question has an answer is
    the gold's to say (the bank's `type` keeps its group there); a semantic one has no gold,
    so for it the bank says."""
    if group(meta["id"]) in ("1", "2"):
        no_answer = gold is not None and gold["type"] == "no_answer"
    else:
        no_answer = meta["type"] == "no_answer"
    return "no_answer" if no_answer else ROUTE[group(meta["id"])]


def score_question(row: dict[str, Any], meta: dict[str, str], gold: dict[str, Any] | None,
                   pools: dict[str, dict[str, Any]], judgments: dict[str, dict[str, dict[str, dict[str, int]]]]
                   ) -> dict[str, Any]:
    """The question's error class, scores and strict passes (per judge where the judge matters)."""
    qid, kind = meta["id"], question_type(meta, gold)
    plan = row["plan"]
    error = error_class(row, meta, gold)
    targets = [t.strip() for t in meta.get("targets", "").split(",") if t.strip()]
    pooled = next(((where, pools[where][qid]) for where in ("rag", "mixed") if qid in pools.get(where, {})),
                  (None, None))
    out: dict[str, Any] = {"id": qid, "type": kind, "group": group(qid), "plan_type": plan["type"] if plan else None,
                           "answer_type": meta["answer_type"], "error_class": error,
                           "error": ERROR_NAMES.get(error, "")}
    broke = row["error_class"] in RUN_ERRORS

    if kind == "no_answer":
        if broke or plan is None:
            empty = False
        elif plan["type"] == "rag":
            empty = abstains(pooled[1], targets or ["publications"])
        else:
            empty = is_empty(row["answer"])
        for judge in JUDGES:
            out[f"score_{judge}"] = out[f"strict_{judge}"] = float(empty)
        return out

    if kind in ("graph", "mixed"):
        scores = compare(row["answer"], gold["answer"])
        out.update(scores)
        out["query"] = query_score(scores, gold["answer"]["type"])
    if kind == "graph":
        exact = scores["em_tol"] if gold["answer"]["type"] == "scalar" else scores["em"]
        for judge in JUDGES:
            out[f"score_{judge}"], out[f"strict_{judge}"] = out["query"], exact
        return out

    for judge in JUDGES:
        # Mixed questions have Luna alone; their numbers stand in the other judge's columns too.
        source = judge if kind == "rag" else "luna"
        where, entry = pooled
        grades = judgments.get(where, {}).get(source, {}).get(qid, {}) if where else {}
        part = rag_part(entry, grades, targets)
        out[f"nDCG@{K}_{judge}"], out[f"F1@{K}_{judge}"] = part["nDCG"], part["F1"]
        for target, metrics in part["kinds"].items():
            for name in (f"P@{K}", f"R@{K}", f"nDCG@{K}", "R2", "zero_share", f"judged@{K}"):
                out[f"{target}_{name}_{judge}"] = metrics.get(name)
        if kind == "rag":
            out[f"score_{judge}"] = (part["nDCG"] + part["F1"]) / 2
            out[f"strict_{judge}"] = float(part["strict"])
        else:
            em_term = scores["em"] if gold["answer"]["type"] == "scalar" else scores["f1"]
            tol_term = scores["em_tol"] if gold["answer"]["type"] == "scalar" else scores["f1"]
            out[f"score_{judge}"] = (em_term + tol_term + part["nDCG"] + part["F1"]) / 4
            out[f"strict_{judge}"] = float(part["strict"] and out["query"] >= STRICT_QUERY)
    return out


# --- the run -------------------------------------------------------------------------------------

def _load_pools(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, dict[str, dict[str, int]]]]]:
    """Pool entries by id and judgments by judge for each pool, people graded from their works."""
    pools, judgments = {}, {}
    for where, judges in POOL_JUDGES.items():
        pool_dir = run_dir / where
        if not (pool_dir / "pool.json").is_file():
            continue
        pool = rag_eval.load_pool(pool_dir)
        pools[where] = {entry["question"]: entry for entry in pool["questions"]}
        judgments[where] = {}
        for judge in judges:
            grades = rag_eval.load_judgments(pool_dir, judge)
            rag_eval.person_grades(pool, grades)
            judgments[where][judge] = grades
            _variant_metrics(pool_dir, pool, grades, judge)
    return pools, judgments


def _variant_metrics(pool_dir: Path, pool: dict[str, Any], grades: dict[str, dict[str, int]], judge: str) -> None:
    """The colleague's per-variant means (as his compute_metrics), with the people graded."""
    rows = []
    for (kind, variant), by_question in sorted(rag_eval.per_question(pool, grades, CUTOFFS).items()):
        values = list(by_question.values())
        rows.append({"kind": kind, "variant": variant, "questions": len(values),
                     **{metric: round(sum(v[metric] for v in values) / len(values), 3) for metric in values[0]}})
    if rows:
        _write_csv(pool_dir / f"metrics_{judge}.csv", rows)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _spread(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p95": None}
    return {"median": round(float(np.median(values)), 3), "p95": round(float(np.percentile(values, 95)), 3)}


def _mean(rows: list[dict[str, Any]], column: str) -> float:
    return round(float(np.mean([r[column] for r in rows])), 3)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per type: score, strict pass rate, error shares, tokens and time; the macro score over the types.

    Only rag questions depend on the judge (two of them); graph and no_answer
    have none, mixed has Luna alone, so those carry one score and one rate.
    The macro score is given per judge, the types without one counting the same in each.
    """
    summary: dict[str, Any] = {"questions": len(rows), "types": {}}
    for kind in TYPES:
        mine = [r for r in rows if r["type"] == kind]
        if not mine:
            continue
        shares = {str(c): round(sum(r["error_class"] == c for r in mine) / len(mine), 3) for c in ERROR_NAMES}
        shares["none"] = round(sum(r["error_class"] is None for r in mine) / len(mine), 3)
        if kind == "rag":
            score = {j: _mean(mine, f"score_{j}") for j in JUDGES}
            strict = {j: _mean(mine, f"strict_{j}") for j in JUDGES}
        else:
            # Any judge's column holds the same number here (see score_question).
            score, strict = _mean(mine, f"score_{JUDGES[0]}"), _mean(mine, f"strict_{JUDGES[0]}")
        summary["types"][kind] = {
            "questions": len(mine),
            **({"judge": "luna"} if kind == "mixed" else {}),
            "score": score,
            "strict_pass_rate": strict,
            "error_shares": shares,
            "tokens": _spread([r["tokens"] for r in mine if r["tokens"] is not None]),
            "seconds": _spread([r["seconds"] for r in mine]),
        }
    present = summary["types"]
    summary["macro_score"] = {
        j: round(float(np.mean([t["score"][j] if isinstance(t["score"], dict) else t["score"]
                                for t in present.values()])), 3)
        for j in JUDGES}
    summary["errors"] = error_table(rows)
    summary["tokens"] = _spread([r["tokens"] for r in rows if r["tokens"] is not None])
    summary["seconds"] = _spread([r["seconds"] for r in rows])
    return summary


ERROR_COLUMNS = (*(str(c) for c in ERROR_NAMES), "none")


def error_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Questions by type (and all together) and error class 1-6 or none: count and share of the row."""
    table = []
    for kind in (*TYPES, "all"):
        mine = rows if kind == "all" else [r for r in rows if r["type"] == kind]
        if not mine:
            continue
        counts = {c: sum((str(r["error_class"]) if r["error_class"] is not None else "none") == c for r in mine)
                  for c in ERROR_COLUMNS}
        table.append({"type": kind, "questions": len(mine), "counts": counts,
                      "shares": {c: round(n / len(mine), 3) for c, n in counts.items()}})
    return table


def write_error_table(path: Path, table: list[dict[str, Any]]) -> None:
    """The error table as a sheet: one row per type, a count and a share per error class."""
    rows = []
    for line in table:
        row: dict[str, Any] = {"type": line["type"], "questions": line["questions"]}
        for c in ERROR_COLUMNS:
            name = "no error" if c == "none" else f"{c} {ERROR_NAMES[int(c)]}"
            row[name] = line["counts"][c]
            row[f"{name} share"] = line["shares"][c]
        rows.append(row)
    _write_csv(path, rows)


def evaluate(name: str) -> dict[str, Any]:
    run_dir = RUNS_DIR / name
    answers = [json.loads(line) for line in (run_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    bank = {row["id"]: row for row in read_bank(QUESTIONS)}
    gold = json.loads(GOLD.read_text(encoding="utf-8"))["questions"]
    pools, judgments = _load_pools(run_dir)
    rows = []
    for row in answers:
        out = score_question(row, bank[row["id"]], gold.get(row["id"]), pools, judgments)
        usage = row["planner"]["usage"] or {}
        out["tokens"] = usage.get("total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                                                      if usage else None)
        # The system's own time: planner, resolution, Cypher (the pools are the evaluation's).
        out["seconds"] = round(sum(v for v in row["seconds"].values() if isinstance(v, (int, float))), 3)
        rows.append(out)
    _write_csv(run_dir / "metrics_questions.csv", rows)
    summary = summarize(rows)
    write_error_table(run_dir / "errors.csv", summary["errors"])
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Metrics of a text-to-Cypher baseline run.")
    parser.add_argument("--run", required=True, help=f"run name, a directory under {RUNS_DIR}")
    args = parser.parse_args()
    print(json.dumps(evaluate(args.run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

