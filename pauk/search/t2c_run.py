"""A run of the text-to-Cypher baseline over the validation bank.

The system sees only each question's text; the bank's labels (type,
answer_type, targets, entities) stay with the evaluation. Three stages,
each writing into the run directory:

  1. plans    - the planner (`llm_baseline`) on every question, appended to
                plans.jsonl as they come; a re-run reuses the good plans made
                with the same model and prompt and asks again for the failed ones;
  2. answers  - entities resolved by the name indexes, then the Cypher of a
                graph question run as is, and that of a mixed question run on
                the items the existing judgments graded >= 1 (the gold Cypher's
                input too, so the answer measures the Cypher alone); answers.jsonl;
  3. pools    - the colleague's pool over every search variant for the questions
                the planner sent to rag or mixed, with the planner's keywords,
                and the judgments copied next to it, ready for his metrics.

Error classes set here: 1 no usable plan, 3 an entity not found, 5 Cypher
rejected by Neo4j (syntax, semantics, missing parameter), 6 timeout or
another Neo4j failure. Classes 2, 4 and 7 need the labels: the metrics set them.

Usage:
    uv run python -m pauk.search.t2c_run --run baseline-1 [--ids q101t1,q152t2] [--no-pool]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from pathlib import Path
from typing import Any

from neo4j import READ_ACCESS, GraphDatabase, unit_of_work
from neo4j.exceptions import ClientError, CypherSyntaxError, DriverError, Neo4jError

from pauk.settings import Settings, settings

from .entities import load_department_index, load_person_index
from .llm import make_client
from .llm_baseline import Plan, plan_question, prompt_id
from .reader import assert_read_only

logger = logging.getLogger(__name__)

BANK_DIR = settings.data_dir / "search" / "validation"
QUESTIONS = BANK_DIR / "questions.csv"
GOLD = BANK_DIR / "gold_graph_mixed.json"
RUNS_DIR = BANK_DIR / "runs"
VARIANTS = settings.data_dir / "rag" / "runs" / "validation" / "variants.txt"
POOL_K = 50

# Judgments by question group; a mixed question gets the items any of its judges graded >= 1.
JUDGMENTS: dict[str, dict[str, Path]] = {
    "2": {"luna": BANK_DIR / "mixed" / "judgments_luna.json"},
    "3": {"luna": settings.data_dir / "rag" / "runs" / "validation" / "judgments_luna_new.json",
          "qwen50": settings.data_dir / "rag" / "runs" / "validation" / "judgments_qwen50_new.json"},
}
MIN_GRADE = 1
# The judges each pool is scored by: two for the semantic questions, Luna alone for the mixed ones.
POOL_JUDGES: dict[str, tuple[str, ...]] = {"rag": ("luna", "qwen50"), "mixed": ("luna",)}

INVALID_PLAN, ENTITY_NOT_FOUND, CYPHER_REJECTED, NEO4J_FAILED = 1, 3, 5, 6


# --- inputs ------------------------------------------------------------------------

def read_bank(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def group(question_id: str) -> str:
    """The group digit of an id like q152t2."""
    return question_id.rsplit("t", 1)[-1]


def snapshot_year(gold_path: Path) -> int:
    """The current year of the graph snapshot, for "за последние N лет"."""
    return int(json.loads(gold_path.read_text(encoding="utf-8"))["snapshot_date"][:4])


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


# --- stage 1: plans ----------------------------------------------------------------------

def make_plans(run_dir: Path, questions: list[dict[str, str]], config: Settings, year: int,
               replan: bool = False) -> dict[str, dict[str, Any]]:
    """A plan record per question id, reusing the good ones of an earlier run of this prompt and model."""
    path = run_dir / "plans.jsonl"
    client = make_client(config)
    pid = prompt_id(year)
    done: dict[str, dict[str, Any]] = {}
    if not replan:
        for record in _read_jsonl(path):
            if record["plan"] and record["model"] == client.model and record["prompt_id"] == pid:
                done[record["id"]] = record
    run_dir.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for number, meta in enumerate(questions, 1):
            if meta["id"] in done:
                continue
            logger.info("[%d/%d] planning %s", number, len(questions), meta["id"])
            result = plan_question(client, meta["question"], year=year)
            record = {"id": meta["id"], "model": client.model, "prompt_id": pid,
                      "plan": result.plan.model_dump() if result.plan else None,
                      "error_class": result.error_class, "error": result.error, "attempts": result.attempts,
                      "raw": result.raw, "usage": result.usage, "seconds": result.seconds}
            # Appended at once: a crash halfway keeps the plans already paid for.
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            done[meta["id"]] = record
    return done


# --- stage 2: answers --------------------------------------------------------------------

def normalize(rows: list[dict[str, Any]], answer_type: str) -> dict[str, Any]:
    """Cypher rows in the contract of the gold: a scalar, a list of first-column values or a table."""
    if answer_type == "scalar":
        return {"type": "scalar", "value": next(iter(rows[0].values())) if rows and rows[0] else None}
    if answer_type == "table":
        columns = list(rows[0]) if rows else []
        return {"type": "table", "columns": columns, "rows": [[row.get(c) for c in columns] for row in rows]}
    return {"type": "list", "value": [next(iter(row.values())) for row in rows if row]}


def judged_items(question_id: str, kind: str, min_grade: int = MIN_GRADE) -> list[str]:
    """Ids of `kind` graded at least `min_grade` by any judge of the question's group."""
    items: set[str] = set()
    prefix = f"{kind}:"
    for path in JUDGMENTS.get(group(question_id), {}).values():
        if not path.is_file():
            continue
        grades = json.loads(path.read_text(encoding="utf-8")).get(question_id, {})
        items |= {key[len(prefix):] for key, grade in grades.items()
                  if key.startswith(prefix) and grade is not None and grade >= min_grade}
    return sorted(items)


class Cypher:
    """Read-only Cypher with a timeout, failures sorted into error classes 5 and 6."""

    def __init__(self, config: Settings, timeout: float) -> None:
        self._driver = GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password))
        self.timeout = timeout

    def close(self) -> None:
        self._driver.close()

    def run(self, text: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, int | None, str | None]:
        """(rows, None, None) or (None, error class, message)."""
        try:
            assert_read_only(text)
        except ValueError as exc:
            return None, CYPHER_REJECTED, str(exc)
        try:
            # In a managed transaction the timeout goes on the transaction function, not on a Query.
            @unit_of_work(timeout=self.timeout)
            def work(tx):
                return tx.run(text, **params).data()

            with self._driver.session(default_access_mode=READ_ACCESS) as session:
                rows = session.execute_read(work)
            return rows, None, None
        except CypherSyntaxError as exc:
            return None, CYPHER_REJECTED, str(exc)[:500]
        except ClientError as exc:
            # Statement.* is the query itself (semantics, types, a missing parameter);
            # the rest - a timed-out transaction, a memory limit - is the database's.
            rejected = (exc.code or "").startswith("Neo.ClientError.Statement.")
            return None, CYPHER_REJECTED if rejected else NEO4J_FAILED, str(exc)[:500]
        except (Neo4jError, DriverError) as exc:
            return None, NEO4J_FAILED, f"{type(exc).__name__}: {str(exc)[:480]}"


def answer_all(questions: list[dict[str, str]], plans: dict[str, dict[str, Any]], config: Settings,
               timeout: float) -> list[dict[str, Any]]:
    indexes = {"person": load_person_index(config.search_dir), "department": load_department_index(config.search_dir)}
    cypher = Cypher(config, timeout)
    out = []
    try:
        for meta in questions:
            record = plans[meta["id"]]
            row: dict[str, Any] = {"id": meta["id"], "question": meta["question"], "plan": record["plan"],
                                   "planner": {key: record[key] for key in ("model", "prompt_id", "attempts", "usage",
                                                                            "seconds", "error")},
                                   "entities": [], "params": {}, "answer": None,
                                   "error_class": record["error_class"], "error": record["error"],
                                   "seconds": {"plan": record["seconds"]}}
            out.append(row)
            if record["plan"] is None:
                continue
            plan = Plan.model_validate(record["plan"])
            start = time.perf_counter()
            for entity in plan.entities:
                resolution = indexes[entity.kind].resolve(entity.name)
                best = resolution.best
                row["entities"].append({**entity.model_dump(), "id": best.id if best else None,
                                        "label": best.label if best else None,
                                        "score": best.score if best else None, "ambiguous": resolution.ambiguous})
                if best is not None:
                    row["params"][entity.param] = best.id
            row["seconds"]["resolve"] = round(time.perf_counter() - start, 3)
            missing = [e["name"] for e in row["entities"] if e["id"] is None]
            if missing:
                row["error_class"], row["error"] = ENTITY_NOT_FOUND, f"not found: {missing}"
                continue
            if plan.type == "rag":
                continue  # answered by the pool (stage 3)
            if plan.type == "mixed":
                row["params"][plan.rag_param] = judged_items(meta["id"], plan.rag_param)
            start = time.perf_counter()
            rows, error_class, error = cypher.run(plan.cypher, row["params"])
            row["seconds"]["cypher"] = round(time.perf_counter() - start, 3)
            if rows is None:
                row["error_class"], row["error"] = error_class, error
                continue
            row["answer"] = normalize(rows, plan.answer_type)
    finally:
        cypher.close()
    return out


# --- stage 3: pools ----------------------------------------------------------------------

def build_pools(run_dir: Path, questions: list[dict[str, str]], plans: dict[str, dict[str, Any]],
                config: Settings) -> dict[str, Any]:
    from pauk.rag.evaluation import build_pool
    from pauk.rag.retrieve import KEYWORDS_PROMPT_ID, Rag

    keywords = {qid: {"text": record["plan"]["rag"]["keywords"], "llm": record["model"], "prompt_id": record["prompt_id"]}
                for qid, record in plans.items() if record["plan"] and record["plan"].get("rag")}
    keywords_path = run_dir / "keywords.json"
    _write_json(keywords_path, keywords)
    variants = [v.strip() for v in VARIANTS.read_text(encoding="utf-8").replace("\n", ",").split(",") if v.strip()]
    summary: dict[str, Any] = {"variants": len(variants), "k": POOL_K, "keywords_prompt_id": KEYWORDS_PROMPT_ID}
    rag = Rag(config)
    try:
        for kind in ("rag", "mixed"):
            chosen = [meta for meta in questions
                      if plans[meta["id"]]["plan"] and plans[meta["id"]]["plan"]["type"] == kind]
            if not chosen:
                continue
            pool_dir = run_dir / kind
            pool_dir.mkdir(parents=True, exist_ok=True)
            # The pool searches what the planner asked for: its targets, not the bank's.
            questions_path = pool_dir / "questions.csv"
            with questions_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["id", "question", "type", "targets", "expected"])
                writer.writeheader()
                for meta in chosen:
                    writer.writerow({"id": meta["id"], "question": meta["question"], "type": meta["type"],
                                     "targets": ",".join(plans[meta["id"]]["plan"]["rag"]["targets"]),
                                     "expected": meta.get("expected", "")})
            start = time.perf_counter()
            # Every group: a question the planner misrouted is pooled where it was sent.
            pool = build_pool(pool_dir, rag, questions_path, variants, POOL_K,
                              groups=("1", "2", "3"), keywords_path=keywords_path)
            for judge in POOL_JUDGES[kind]:
                merged: dict[str, Any] = {}
                for paths in JUDGMENTS.values():
                    if judge in paths and paths[judge].is_file():
                        merged.update(json.loads(paths[judge].read_text(encoding="utf-8")))
                _write_json(pool_dir / f"judgments_{judge}.json", merged)
            summary[kind] = {"questions": len(pool["questions"]), "seconds": round(time.perf_counter() - start, 1)}
    finally:
        rag.close()
    return summary


# --- the run --------------------------------------------------------------------------------

def run(name: str, ids: list[str] | None = None, *, pools: bool = True, replan: bool = False,
        timeout: float = 30.0, config: Settings = settings) -> dict[str, Any]:
    run_dir = RUNS_DIR / name
    questions = read_bank(QUESTIONS)
    if ids:
        unknown = set(ids) - {q["id"] for q in questions}
        if unknown:
            raise SystemExit(f"not in the bank: {sorted(unknown)}")
        questions = [q for q in questions if q["id"] in ids]
    year = snapshot_year(GOLD)
    plans = make_plans(run_dir, questions, config, year, replan=replan)
    answers = answer_all(questions, plans, config, timeout)
    with (run_dir / "answers.jsonl").open("w", encoding="utf-8") as fh:
        for row in answers:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    summary: dict[str, Any] = {"run": str(run_dir), "questions": len(answers), "year": year,
                               "errors": {str(c): sum(1 for a in answers if a["error_class"] == c) for c in (1, 3, 5, 6)},
                               "types": {t: sum(1 for a in answers if a["plan"] and a["plan"]["type"] == t)
                                         for t in ("graph", "mixed", "rag")}}
    if pools:
        summary["pools"] = build_pools(run_dir, questions, plans, config)
    _write_json(run_dir / "run.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the text-to-Cypher baseline over the validation bank.")
    parser.add_argument("--run", required=True, help=f"run name, a directory under {RUNS_DIR}")
    parser.add_argument("--ids", default=None, help="comma-separated question ids (default: the whole bank)")
    parser.add_argument("--no-pool", action="store_true", help="skip stage 3, the pools over every search variant")
    parser.add_argument("--replan", action="store_true", help="ask the planner again even for good earlier plans")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per Cypher query")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ids = [part.strip() for part in args.ids.split(",") if part.strip()] if args.ids else None
    print(json.dumps(run(args.run, ids, pools=not args.no_pool, replan=args.replan, timeout=args.timeout),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
