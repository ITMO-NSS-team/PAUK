"""Run a list of questions through the search and lay the results out for review.

Two files come out: `results.jsonl` with every answer in full (plan, entity
candidates, entry points, sections, notes, timings) and `review.csv` with one
row per question - what the system understood and the top of each section -
plus empty columns for a person to mark whether it was right and, if not,
at which step it broke. The CSV opens in Excel or Google Sheets as is.

Answers already in `results.jsonl` are reused unless --force: re-running
after adding a few questions costs tokens only for the new ones.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pauk.settings import Settings

from .pipeline import AskOptions, Engine

logger = logging.getLogger(__name__)

TOP = 3
# Filled in by hand while reviewing; the stage names follow the pipeline so
# the failures can be counted per stage afterwards.
REVIEW_COLUMNS = ("correct", "broken_step", "comment")
BROKEN_STEPS = "parse | entities | retrieval | template | ranking | answer | data | unsupported"


def read_questions(path: Path) -> list[dict[str, str]]:
    """Questions from a .txt (one per line, # for comments) or a .csv with a `question` column.

    A CSV may also carry `type` (the question category, for grouping the
    results) and `expected` (what a right answer contains, for the reviewer).
    """
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(fh)]
        if rows and "question" not in rows[0]:
            raise ValueError(f"{path} has no 'question' column")
        questions = [row for row in rows if row.get("question")]
    else:
        questions = [
            {"question": line.strip()}
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    seen: set[str] = set()
    unique = []
    for row in questions:
        if row["question"] not in seen:
            seen.add(row["question"])
            unique.append(row)
    return unique


def _settings_key(options: AskOptions) -> dict[str, Any]:
    keep = ("retrieval_mode", "ranking", "llm_parse", "llm_answer", "top_n")
    return {key: value for key, value in asdict(options).items() if key in keep}


def _load_cached(path: Path, options: AskOptions) -> dict[str, dict[str, Any]]:
    """Earlier answers to reuse; only those obtained with the same options."""
    if not path.exists():
        return {}
    cached = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("batch_options") == _settings_key(options) and "error" not in row:
            cached[row["question"]] = row
    return cached


def run(questions_path: Path, out_dir: Path, options: AskOptions, *, force: bool = False,
        engine: Engine | None = None, config: Settings | None = None) -> dict[str, Any]:
    questions = read_questions(questions_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    cached = {} if force else _load_cached(results_path, options)
    own_engine = engine is None
    results = []
    try:
        for number, meta in enumerate(questions, 1):
            question = meta["question"]
            if question in cached:
                result = cached[question]
            else:
                if engine is None:
                    engine = Engine(config)
                logger.info("[%d/%d] %s", number, len(questions), question)
                try:
                    result = engine.ask(question, options)
                except Exception as exc:  # one broken question must not lose the whole batch
                    logger.exception("question failed: %s", question)
                    result = {"question": question, "error": f"{type(exc).__name__}: {exc}"}
                result["batch_options"] = _settings_key(options)
            result["meta"] = meta
            results.append(result)
    finally:
        if own_engine and engine is not None:
            engine.close()

    with results_path.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    review_path = out_dir / "review.csv"
    write_review(results, review_path)
    return {
        "questions": len(results),
        "reused": sum(1 for r in results if r["question"] in cached),
        "errors": sum(1 for r in results if "error" in r),
        "results": str(results_path),
        "review": str(review_path),
    }


def _labels(items: list[dict[str, Any]] | None) -> str:
    return "\n".join(f"{item.get('label')} ({item['score']:.2f})" if isinstance(item.get("score"), float)
                     else str(item.get("label")) for item in (items or [])[:TOP])


def review_row(number: int, result: dict[str, Any]) -> dict[str, Any]:
    meta = result.get("meta") or {}
    row: dict[str, Any] = {
        "n": number,
        "type": meta.get("type", ""),
        "question": result["question"],
        "expected": meta.get("expected", ""),
    }
    if "error" in result:
        row["notes"] = result["error"]
        return row
    plan = result.get("plan") or {}
    parse = (result.get("trace") or {}).get("parse") or {}
    sections = result.get("sections") or {}
    counts = sections.get("counts") or {}
    row.update({
        "plan_type": plan.get("type"),
        "graph_type": plan.get("graph_type"),
        "core": " | ".join(plan.get("core") or []),
        "entities": "; ".join(f"{e['kind']}: {e['name']}" for e in plan.get("entities") or []),
        "filter": json.dumps({k: v for k, v in (plan.get("filter") or {}).items() if v not in (None, True)},
                             ensure_ascii=False) if plan.get("filter") else "",
        "sections": ", ".join(plan.get("expected_values") or []),
        "resolved": "; ".join(
            f"{e['query']} → {e['candidates'][0]['label'] if e['candidates'] else 'не найдено'}"
            + (" (неоднозначно)" if e.get("ambiguous") else "")
            for e in result.get("entities") or []
        ),
        "llm_fallback": parse.get("fallback", ""),
        "top_entry_points": "\n".join(f"{h['title']} ({h['score']:.2f})" for h in (result.get("entry_points") or [])[:TOP]),
        "persons": _labels(sections.get("persons")),
        "departments": _labels(sections.get("departments")),
        "publications": _labels(sections.get("publications")),
        "repositories": _labels(sections.get("repositories")),
        "counts": json.dumps({k: v for k, v in counts.items() if k != "by_year"}, ensure_ascii=False)
        if counts else "",
        "answer": result.get("answer") or "",
        "notes": "\n".join(result.get("notes") or []),
        "seconds": round(sum((result.get("timings") or {}).values()), 2),
    })
    return row


COLUMNS = ("n", "type", "question", "expected", "plan_type", "graph_type", "core", "entities", "filter",
           "sections", "resolved", "llm_fallback", "top_entry_points", "persons", "departments",
           "publications", "repositories", "counts", "answer", "notes", "seconds", *REVIEW_COLUMNS)


def _earlier_marks(path: Path) -> dict[str, dict[str, str]]:
    """Marks already made in an earlier review.csv, so a re-run does not wipe them."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {
            row["question"]: {key: row.get(key) or "" for key in REVIEW_COLUMNS}
            for row in csv.DictReader(fh) if row.get("question")
        }


def write_review(results: list[dict[str, Any]], path: Path) -> None:
    marks = _earlier_marks(path)
    # utf-8-sig: without the BOM Excel opens Cyrillic as mojibake.
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for number, result in enumerate(results, 1):
            row = review_row(number, result)
            row.update(marks.get(result["question"], {}))
            writer.writerow(row)
