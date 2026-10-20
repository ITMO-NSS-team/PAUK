"""Validation: pooled relevance judgments, metrics and ablation comparisons.

A *variant* is an index profile and a retrieval mode, written `full:hybrid`,
optionally with `:rrf` (fusion), `:en` (BM25 on the extracted English keywords) or both, `:rrf+en`. Every variant answers every
question; the top `k` people, publications and repositories of all
variants are merged into one pool per question, and only the pool is
judged: 0 - not relevant, 1 - partly, 2 - relevant (TREC-style pooling;
nobody can judge 13 thousand papers per question). What no variant
returned counts as not relevant, so P@k, nDCG@k and MRR are comparable
across variants. Recall@k is pooled recall: its denominator is the relevant
items any variant found, not every relevant item in the graph, so it
overstates true recall - it compares variants, it does not measure coverage.
Cutoffs are chosen at `compute_metrics` and cannot exceed the pool's `k`:
beyond it there are neither rankings nor judgments.

An ablation is then `compare`: the per-question nDCG of each variant
against a baseline, with a two-sided sign test for whether the difference
is more than chance. Files live in the run directory: `pool.json`,
`judgments.json` (or `judgments_<judge>.json`), `metrics.csv`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np

from .models import get as get_model
from .readme import clean_readme, load_readme
from .retrieve import MODES, Hit, Rag

K = 10
# The model of a variant written without `@model`; see parse_variant.
VARIANT_DEFAULT_MODEL = "bge"
KINDS = ("persons", "publications", "repositories")
GRADES = (0, 1, 2)
SNIPPET_CHARS = 700
# How much of a paper's full text and of a README the judge reads (the judging prompt).
JUDGE_TEXT_CHARS = 3000
_JUDGE = re.compile(r"[a-z0-9_]{1,32}")

PUBLICATIONS_FOR_JUDGE = """
UNWIND $ids AS id MATCH (p:Publication {id: id})
RETURN p.id AS id, p.title AS title, p.year AS year, p.journal AS journal, p.abstract AS abstract,
       p.doi AS doi, p.openalex_url AS openalex_url, left(coalesce(p.full_text, ''), $text_chars) AS full_text,
       [(a:Person)-[r:AUTHORED]->(p) | {name: coalesce(a.name_ru, a.name_en, a.name_raw), position: r.position}]
         AS authors
"""
REPOSITORIES_FOR_JUDGE = """
UNWIND $ids AS id MATCH (r:Repository {id: id})
RETURN r.id AS id, r.name AS name, r.url AS url, r.description AS description, r.stars_num AS stars,
       [(r)-[:IMPLEMENTS]->(p:Publication) | p.title] AS implemented
"""


def read_questions(path: Path) -> list[dict[str, str]]:
    """Rows of a CSV with a `question` column; other columns travel along as `meta`."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(fh)]
    if rows and "question" not in rows[0]:
        raise ValueError(f"{path} has no 'question' column")
    seen, out = set(), []
    for row in rows:
        if row.get("question") and row["question"] not in seen:
            seen.add(row["question"])
            out.append(row)
    return out


def targets(meta: dict[str, str]) -> tuple[str, ...]:
    """Kinds judged for a question (`targets` column); all kinds when it is empty.

    A topic question has hardly any relevant repository, and a question
    about code is answered by repositories: pooling the other kind there
    would cost hundreds of judgments that are almost all zeros.
    """
    wanted = [kind.strip() for kind in (meta.get("targets") or "").split(",") if kind.strip()]
    unknown = set(wanted) - set(KINDS)
    if unknown:
        raise ValueError(f"unknown targets {sorted(unknown)} for {meta.get('question')!r}; known: {KINDS}")
    return tuple(kind for kind in KINDS if kind in wanted) or KINDS


def parse_variant(text: str) -> dict[str, Any]:
    """`profile[@model]:mode[:options]`, options joined by `+`: `rrf` (fusion), `en` (BM25 on the
    keywords), `kw` (the encoder too gets the keywords instead of the question).

    Without `@model` the variant means bge-m3 - not the configured default -
    so variants judged before models were selectable keep their meaning when
    the default changes.
    """
    parts = text.split(":")
    if len(parts) not in (2, 3) or parts[1] not in MODES:
        raise ValueError(f"variant {text!r}: profile[@model]:mode[:rrf+en+kw], mode one of {MODES}")
    profile, _, model = parts[0].partition("@")
    options = set(parts[2].split("+")) if len(parts) == 3 else set()
    if options - {"rrf", "en", "kw"}:
        raise ValueError(f"variant {text!r}: unknown options {sorted(options - {'rrf', 'en', 'kw'})}")
    return {"profile": profile, "model": model or VARIANT_DEFAULT_MODEL, "mode": parts[1], "fusion": "rrf" if "rrf" in options else "minmax",
            "bm25_keywords": "en" in options, "dense_keywords": "kw" in options}


def _shuffled(question: str, ids: list[str]) -> list[str]:
    """A fixed order that hides which variant ranked an item where."""
    return sorted(ids, key=lambda i: hashlib.sha1(f"{question}\0{i}".encode()).hexdigest())


def _snippet(text: str | None) -> str:
    text = (text or "").strip()
    return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")


# --- pool -------------------------------------------------------------------------

def build_pool(run_dir: Path, rag: Rag, questions_path: Path, variants: list[str], k: int = K) -> dict[str, Any]:
    specs = {name: parse_variant(name) for name in variants}
    questions = read_questions(questions_path)
    # Variants of one model run back to back, so each model is loaded once.
    retrieved: dict[tuple[str, str], dict[str, list[Hit]]] = {}
    for name in sorted(specs, key=lambda n: specs[n]["model"] or rag.model):
        spec = specs[name]
        for meta in questions:
            question = meta["question"]
            bm25_query = rag.extract_keywords(question) if spec["bm25_keywords"] and spec["mode"] != "dense" else None
            dense_query = rag.extract_keywords(question) if spec["dense_keywords"] and spec["mode"] != "bm25" else None
            # Deeper than k: the people are aggregated from these items.
            retrieved[name, question] = rag.retrieve(question, profile=spec["profile"], model=spec["model"],
                                                     mode=spec["mode"], fusion=spec["fusion"],
                                                     bm25_query=bm25_query, dense_query=dense_query,
                                                     k=max(50, k))
    entries = []
    for meta in questions:
        question = meta["question"]
        rankings: dict[str, dict[str, list[str]]] = {}
        persons: dict[str, dict[str, Any]] = {}
        for name in specs:
            hits = retrieved[name, question]
            people = rag.experts(hits, top_n=k)
            rankings[name] = {
                "persons": [p["id"] for p in people],
                "publications": [h.id for h in hits.get("Publication", [])[:k]],
                "repositories": [h.id for h in hits.get("Repository", [])[:k]],
                # The raw best match per label, for judging empty answers.
                "top_dense": {label: max((h.dense for h in items if h.dense is not None), default=None)
                              for label, items in hits.items()},
            }
            for p in people:
                seen = persons.setdefault(p["id"], {"label": p["label"], "url": p["url"], "is_itmo": p["is_itmo"],
                                                    "departments": p["departments"], "evidence": [], "code": [],
                                                    "works": {"publications": [], "repositories": []}})
                # Every work the person was found by, for their grade (person_grades).
                for kind, ids in (("publications", p["publications"]), ("repositories", p["repositories"])):
                    seen["works"][kind] += [i for i in ids if i not in seen["works"][kind]]
                for pub_id in p["publications"][:3]:
                    if pub_id not in seen["evidence"]:
                        seen["evidence"].append(pub_id)
                # Some people reach the top through code alone; without the
                # repositories the judge would see an empty line.
                for repo_id in p["repositories"][:3]:
                    if repo_id not in seen["code"]:
                        seen["code"].append(repo_id)
        judged = targets(meta)
        for ranking in rankings.values():
            for kind in KINDS:
                if kind not in judged:
                    ranking[kind] = []
        if "persons" not in judged:
            persons = {}
        items = {"persons": persons, **_item_details(rag, rankings)}
        # Works that put a person in some variant's top but are not judged as
        # results themselves - repositories in a question about people, or a
        # paper below the pool depth. The judge grades them too, so that every
        # person's grade rests on judged works.
        outside = {kind: sorted({i for p in persons.values() for i in p["works"][kind]} - items[kind].keys())
                   for kind in ("publications", "repositories")}
        extra = _item_details(rag, {"outside": outside}) if any(outside.values()) else {}        # A person is judged by what they wrote: show the titles behind the score.
        titles = {i: d["title"] for i, d in items["publications"].items()}
        missing = {i for p in persons.values() for i in p["evidence"]} - titles.keys()
        titles.update({r["id"]: r["title"] for r in rag.graph.read(PUBLICATIONS_FOR_JUDGE, ids=sorted(missing), text_chars=0)})
        code_ids = sorted({i for p in persons.values() for i in p["code"]})
        names = {r["id"]: r["name"] for r in rag.graph.read(REPOSITORIES_FOR_JUDGE, ids=code_ids)}
        for p in persons.values():
            p["evidence"] = [titles.get(i) or i for i in p["evidence"][:5]]
            p["code"] = [names.get(i) or i for i in p["code"][:5]]
        entries.append({"question": question, "meta": meta, "rankings": rankings, "items": items, "extra": extra,
                        "order": {kind: _shuffled(question, list(items[kind])) for kind in KINDS}})
    pool = {"k": k, "variants": specs, "kinds": list(KINDS), "questions": entries}
    add_person_context(rag.graph, pool)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "pool.json", pool)
    return pool


def _item_details(rag: Rag, rankings: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, dict[str, Any]]]:
    pub_ids = sorted({i for r in rankings.values() for i in r["publications"]})
    repo_ids = sorted({i for r in rankings.values() for i in r["repositories"]})
    publications = {}
    for row in rag.graph.read(PUBLICATIONS_FOR_JUDGE, ids=pub_ids, text_chars=JUDGE_TEXT_CHARS):
        authors = [a["name"] for a in sorted(row["authors"], key=lambda a: a["position"] or 0)]
        doi = row.get("doi")
        publications[row["id"]] = {
            "title": row["title"], "year": row["year"], "journal": row["journal"],
            "url": (doi if doi and doi.startswith("http") else f"https://doi.org/{doi}") if doi else row["openalex_url"],
            "authors": authors[:8] + (["…"] if len(authors) > 8 else []),
            "abstract": (row["abstract"] or "").strip()[:JUDGE_TEXT_CHARS], "full_text": row["full_text"] or "",
        }
    repositories = {}
    for row in rag.graph.read(REPOSITORIES_FOR_JUDGE, ids=repo_ids):
        repositories[row["id"]] = {
            "title": row["name"], "url": row["url"], "description": row["description"], "stars": row["stars"],
            "implemented": row["implemented"][:3],
            "readme": _snippet(clean_readme(load_readme(rag.config, row["id"]) or "")),
            # The judge reads the README as written, not as our cleaning left it:
            # the README ablation must not be judged through its own preprocessing.
            "readme_full": (load_readme(rag.config, row["id"]) or "")[:JUDGE_TEXT_CHARS],
        }
    return {"publications": publications, "repositories": repositories}


PERSON_WORKS = """
UNWIND $rows AS row
MATCH (:Person {id: row.person})-[:AUTHORED]->(p:Publication) WHERE p.title IN row.titles
RETURN row.person AS person, p.id AS id, p.title AS title, p.year AS year, p.abstract AS abstract
"""


def add_person_context(graph: Any, pool: dict[str, Any]) -> int:
    """Give each person the ids of their shown works, and the works an abstract, for the judging page.

    A title often does not tell what a paper is about, and a judge should
    not have to open every profile; the page shows the abstracts under the
    person, along with the grade the judge already gave the paper. The pool
    keeps titles only, so the works are found again by person and title -
    exact, since both come from the graph. Returns the works added.
    """
    added = 0
    for entry in pool["questions"]:
        persons = entry["items"].get("persons", {})
        rows = [{"person": pid, "titles": p["evidence"]} for pid, p in persons.items() if p.get("evidence")]
        works: dict[str, dict[str, Any]] = {}
        ids: dict[str, dict[str, str]] = {pid: {} for pid in persons}
        for row in graph.read(PERSON_WORKS, rows=rows) if rows else []:
            ids[row["person"]].setdefault(row["title"], row["id"])
            works[row["id"]] = {"title": row["title"], "year": row["year"], "abstract": _snippet(row["abstract"])}
        for pid, p in persons.items():
            p["evidence_ids"] = [ids[pid][t] for t in p.get("evidence", []) if t in ids[pid]]
        entry["context"] = {"publications": works}
        added += len(works)
    return added


def load_pool(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "pool.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


# --- judgments --------------------------------------------------------------------

def judgments_path(run_dir: Path, judge: str | None) -> Path:
    """`judgments.json` for the person reviewing; `judgments_<judge>.json` for anyone else.

    A second judge (another person, or a model) never writes into the main
    file, so their grades can be compared with it instead of mixed in.
    """
    if judge is None:
        return run_dir / "judgments.json"
    if not _JUDGE.fullmatch(judge):
        raise ValueError("judge name: lowercase letters, digits and _")
    return run_dir / f"judgments_{judge}.json"


def load_judgments(run_dir: Path, judge: str | None = None) -> dict[str, dict[str, int]]:
    path = judgments_path(run_dir, judge)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_judgment(run_dir: Path, question: str, kind: str, item_id: str, grade: int | None,
                  judge: str | None = None) -> None:
    """Set or (grade None) clear one grade. Keys are stable, so a rebuilt pool keeps them."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if grade is not None and grade not in GRADES:
        raise ValueError(f"grade must be one of {GRADES}")
    entry = next((q for q in load_pool(run_dir)["questions"] if q["question"] == question), None)
    if entry is None or item_id not in entry["items"].get(kind, {}):
        raise KeyError(f"{kind} {item_id} is not in the pool of this question")
    judgments = load_judgments(run_dir, judge)
    grades = judgments.setdefault(question, {})
    if grade is None:
        grades.pop(f"{kind}:{item_id}", None)
    else:
        grades[f"{kind}:{item_id}"] = grade
    _write_json(judgments_path(run_dir, judge), judgments)


def seed_judgments(run_dir: Path, source: Path, judge: str | None = None) -> int:
    """Copy grades from an earlier run for the same questions and items, without overwriting.

    Keys are `<kind>:<node id>` per question text, so grades made in the
    prototype (`data/search/.../judgments*.json`) carry over as they are.
    """
    pool = load_pool(run_dir)
    earlier = json.loads(source.read_text(encoding="utf-8"))
    judgments = load_judgments(run_dir, judge)
    added = 0
    for entry in pool["questions"]:
        grades = judgments.setdefault(entry["question"], {})
        for key, grade in (earlier.get(entry["question"]) or {}).items():
            kind, _, item = key.partition(":")
            if kind in KINDS and item in entry["items"][kind] and key not in grades:
                grades[key] = grade
                added += 1
    _write_json(judgments_path(run_dir, judge), judgments)
    return added


# The judging prompt, as written for the study (2026-10-16): one item per
# request, so grades cannot depend on neighbours. The paragraph on people and
# the pool depth are carried out by code - `person_grades` and the pool's k -
# and are kept in the text so that the published prompt is the one used.
JUDGE_SYSTEM = """Судья получает: вопрос, правило релевантности (что считается ответом) и один элемент (элемент - одна публикация или один репозиторий из пула этого вопроса; люди напрямую не оцениваются).
Оценка предполагает одну из цифр - 0-2:
2 — работа прямо отвечает на вопрос и выполняет правило;
1 — смежная тема, общий метод или инструмент вместо специального, тема затронута, но не главная;
0 — не по теме, совпадают только слова.

Публикация оценивается по: названию, аннотации, первым 3000 символам полного текста (если есть).
Репозиторий оценивается по: названию, описанию, первым 3000 символам README.
Оценка человека = максимум оценок работ (статьи по AUTHORED, репозитории по CONTRIBUTED_TO), по которым он связан с выдачей. Работы, по которым система нашла человека, но которых нет в пуле, размечаются по тем же правилам. Человек без связанных работ с оценкой 1 или 2 получает 0.

Верни JSON: {"grade": 0, 1 или 2}"""
JUDGE_PROMPT_ID = hashlib.sha1(JUDGE_SYSTEM.encode()).hexdigest()[:8]


def judge_system(depth: int | None) -> str:
    """The prompt with the pool depth stated, as the methodology reads; None gives the text the tails were judged with."""
    if depth is None:
        return JUDGE_SYSTEM
    line = f"При разметке берутся первые {depth} элементов списка каждого варианта поиска."
    return JUDGE_SYSTEM.replace("\n\nВерни JSON", f"\n{line}\n\nВерни JSON")
JUDGE_MAX_TOKENS = 50
JUDGED_KINDS = ("publications", "repositories")


def judge_message(entry: dict[str, Any], kind: str, item: dict[str, Any]) -> str:
    """The user message for one item: the question, its rule and the fields the prompt names."""
    if kind == "publications":
        element = {"тип": "публикация", "название": item.get("title"), "аннотация": item.get("abstract") or "",
                   "полный текст (начало)": item.get("full_text") or ""}
    else:
        element = {"тип": "репозиторий", "название": item.get("title"), "описание": item.get("description") or "",
                   "README (начало)": item.get("readme_full") or ""}
    return json.dumps({"вопрос": entry["question"], "правило релевантности": entry["meta"].get("expected", ""),
                       "элемент": element}, ensure_ascii=False, indent=1)


def judge_pool(run_dir: Path, make_llm: Any, judge: str, workers: int = 8,
               state_depth: bool = True, reasoning_effort: str | None = None,
               limit: int | None = None) -> dict[str, Any]:
    """Grade every publication and repository of the pool, one item per request, in parallel.

    Everything an outside reviewer needs is kept next to the grades:
    `judge_<judge>.config.json` (model, prompt, prompt id, limits) and
    `judge_<judge>.log.jsonl`, one line per request with the exact message,
    the raw reply and the token usage. Already graded items are skipped, so a
    rerun after a failure, or after variants are added to the pool, pays only
    for what is new. `make_llm` builds one client per worker thread: a
    requests session is not meant to be shared between threads.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime

    pool = load_pool(run_dir)
    system = judge_system(pool.get("k") if state_depth else None)
    prompt_id = hashlib.sha1(system.encode()).hexdigest()[:8]
    judgments = load_judgments(run_dir, judge)
    todo = [(entry, kind, item_id) for entry in pool["questions"] for kind in JUDGED_KINDS
            for item_id in [*entry["order"].get(kind, []), *entry.get("extra", {}).get(kind, {})]
            if f"{kind}:{item_id}" not in judgments.get(entry["question"], {})]
    if limit is not None:  # a probe: a few items to see the replies and the token cost first
        todo = todo[:limit]
    # A reasoning model thinks within max_tokens: give it room, or the reply is empty.
    max_tokens = JUDGE_MAX_TOKENS if reasoning_effort is None else JUDGE_MAX_TOKENS + 2000
    reasoning = None if reasoning_effort is None else {"effort": reasoning_effort}
    local = threading.local()
    model = make_llm().model
    config_path = run_dir / f"judge_{judge}.config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    if config and (config.get("model") != model or config.get("prompt_id") != prompt_id):
        # One judgments file, one judge: mixing prompts or models in it would
        # make the grades incomparable without anyone noticing.
        raise ValueError(f"{config_path.name} was made by {config.get('model')} with prompt "
                         f"{config.get('prompt_id')}; use another --judge name")
    config = {"model": model, "prompt_id": prompt_id, "system_prompt": system, "temperature": 0,
              "max_tokens": max_tokens, "reasoning": reasoning,
              "text_chars": JUDGE_TEXT_CHARS, "one_item_per_request": True,
              "pool_k": pool.get("k"), "variants": list(pool.get("variants", {})),
              "runs": [*config.get("runs", []), {"started": datetime.now(UTC).isoformat(), "todo": len(todo)}]}
    _write_json(config_path, config)

    def grade(task):
        entry, kind, item_id = task
        if not hasattr(local, "llm"):
            local.llm = make_llm()
        item = entry["items"][kind].get(item_id) or entry["extra"][kind][item_id]
        message = judge_message(entry, kind, item)
        start = time.perf_counter()
        raw = local.llm.chat_json(message, system_prompt=system, max_tokens=max_tokens, reasoning=reasoning)
        value = raw.get("grade") if isinstance(raw, dict) else None
        # bool is an int too; a model answering true/false has not graded.
        ok = isinstance(value, int) and not isinstance(value, bool) and value in GRADES
        record = {"question": entry["question"], "kind": kind, "id": item_id, "model": model,
                  "prompt_id": prompt_id, "message": message, "reply": raw,
                  "grade": value if ok else None, "error": None if ok else (local.llm.last_error or "no grade"),
                  "usage": local.llm.last_usage, "seconds": round(time.perf_counter() - start, 2)}
        return entry["question"], f"{kind}:{item_id}", (value if ok else None), record

    done = failed = 0
    tokens = Counter()
    began = time.perf_counter()
    progress_path = run_dir / f"judge_{judge}.progress.log"

    def report(n: int) -> None:
        # A human-readable line to follow a long run (tail -f), next to the full log.
        rate = n / max(time.perf_counter() - began, 1e-9)
        left = (len(todo) - n) / rate / 60 if rate else 0
        with progress_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} {n}/{len(todo)} graded {done} failed {failed} "
                     f"{rate:.1f}/s ~{left:.0f} min left, tokens {tokens['prompt_tokens']}+"
                     f"{tokens['completion_tokens']}\n")

    with (run_dir / f"judge_{judge}.log.jsonl").open("a", encoding="utf-8") as log, \
            ThreadPoolExecutor(max_workers=workers) as executor:
        for n, future in enumerate(as_completed(executor.submit(grade, t) for t in todo), 1):
            question, key, value, record = future.result()
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            for name in ("prompt_tokens", "completion_tokens"):
                tokens[name] += (record["usage"] or {}).get(name) or 0
            details = (record["usage"] or {}).get("completion_tokens_details") or {}
            tokens["reasoning_tokens"] += details.get("reasoning_tokens") or 0
            if value is None:
                failed += 1
            else:
                judgments.setdefault(question, {})[key] = value
                done += 1
            if n % 50 == 0:  # a crash loses at most 50 grades; the log keeps them all
                log.flush()
                _write_json(judgments_path(run_dir, judge), judgments)
                report(n)
    report(len(todo))
    _write_json(judgments_path(run_dir, judge), judgments)
    config["runs"][-1].update({"finished": datetime.now(UTC).isoformat(), "graded": done,
                               "failed": failed, **tokens})
    _write_json(config_path, config)
    return {"graded": done, "failed": failed, **tokens}


def person_grades(pool: dict[str, Any], judgments: dict[str, dict[str, int]]) -> dict[str, int]:
    """A person's grade is the highest grade among the works they were found by; 0 when none is relevant.

    Set in place as `persons:<id>`, so the metrics treat people like any
    other kind. Works the judge failed on count as ungraded, not as 0, and
    are reported: a person resting only on such works keeps no grade.
    """
    counts = Counter()
    for entry in pool["questions"]:
        grades = judgments.setdefault(entry["question"], {})
        for person_id, person in entry["items"].get("persons", {}).items():
            works = [f"{kind}:{i}" for kind, ids in person.get("works", {}).items() for i in ids]
            known = [grades[w] for w in works if w in grades]
            counts["ungraded_works"] += len(works) - len(known)
            if not known:
                counts["no_grade"] += 1
                continue
            grades[f"persons:{person_id}"] = max(known)
            counts[f"grade_{max(known)}"] += 1
    return dict(counts)


def tails(pool: dict[str, Any], judgments: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    """For every question, kind and variant: the rank of the last item graded 1 or 2.

    The pool depth for the full judging is chosen from these: below the
    deepest relevant item of any variant there is nothing left to find.
    `depth` is how far the variant's list went, so a last relevant rank at
    the depth means the ceiling cut the tail and should be raised.
    """
    rows = []
    for entry in pool["questions"]:
        grades = judgments.get(entry["question"], {})
        for kind in JUDGED_KINDS:
            for variant, ranking in entry["rankings"].items():
                ranked = ranking.get(kind) or []
                if not ranked:
                    continue
                relevant = [rank for rank, i in enumerate(ranked, 1) if grades.get(f"{kind}:{i}", 0) >= 1]
                rows.append({"question": entry["question"], "type": entry["meta"].get("type", ""), "kind": kind,
                             "variant": variant, "depth": len(ranked), "relevant": len(relevant),
                             "last_relevant": max(relevant, default=0),
                             "unjudged": sum(f"{kind}:{i}" not in grades for i in ranked)})
    return rows


def progress(pool: dict[str, Any], judgments: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    out = []
    for entry in pool["questions"]:
        keys = {f"{kind}:{item}" for kind in pool["kinds"] for item in entry["items"][kind]}
        judged = keys & set(judgments.get(entry["question"], {}))
        out.append({"question": entry["question"], "judged": len(judged), "total": len(keys)})
    return out


# --- metrics ----------------------------------------------------------------------

def _dcg(gains: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(rank + 2) for rank, g in enumerate(gains))


def score_ranking(ranked: list[str], grades: dict[str, int], cutoffs: tuple[int, ...]) -> dict[str, float]:
    """P@k, R@k and nDCG@k for each cutoff, MRR and judged@k at the largest one.

    Unjudged items count as not relevant. Recall is over the judged relevant
    items of the question (pooled recall), the only total that is known.
    """
    total = sum(g >= 1 for g in grades.values())
    ideal_order = sorted(grades.values(), reverse=True)
    out: dict[str, float] = {}
    for k in cutoffs:
        gains = [grades.get(item, 0) for item in ranked[:k]]
        hits = sum(g >= 1 for g in gains)
        ideal = _dcg(ideal_order[:k])
        out[f"P@{k}"] = hits / k
        out[f"R@{k}"] = hits / total if total else 0.0
        out[f"nDCG@{k}"] = _dcg(gains) / ideal if ideal else 0.0
    k = max(cutoffs)
    first = next((rank for rank, item in enumerate(ranked[:k], 1) if grades.get(item, 0) >= 1), None)
    out["MRR"] = 1 / first if first else 0.0
    out[f"judged@{k}"] = sum(item in grades for item in ranked[:k]) / k
    return out


def cutoffs_for(pool: dict[str, Any], cutoffs: tuple[int, ...] | None) -> tuple[int, ...]:
    """The requested cutoffs, sorted; default (5, pool k). None may exceed the pool's k."""
    k = pool["k"]
    chosen = tuple(sorted(set(cutoffs or (min(5, k), k))))
    if not chosen or chosen[0] < 1 or chosen[-1] > k:
        raise ValueError(f"cutoffs {chosen} must lie in 1..{k}: the pool holds each variant's top {k}; "
                         f"rebuild it with `pauk rag pool --k N` (and judge the new items) for deeper cutoffs")
    return chosen


def per_question(pool: dict[str, Any], judgments: dict[str, dict[str, int]],
                 cutoffs: tuple[int, ...] | None = None) -> dict[tuple[str, str], dict[str, dict]]:
    """{(kind, variant): {question: metrics}} over questions that have a relevant item of that kind.

    A question whose pool holds no relevant repository says nothing about
    repository search - every variant would score 0 - so it is left out of
    that kind instead of dragging all variants down alike.
    """
    cutoffs = cutoffs_for(pool, cutoffs)
    out: dict[tuple[str, str], dict[str, dict]] = {}
    for entry in pool["questions"]:
        for kind in pool["kinds"]:
            # Only the kinds the question asks for: repositories graded as the
            # works of people in a question about people are not its results.
            if kind not in targets(entry["meta"]):
                continue
            grades = {key.split(":", 1)[1]: g for key, g in judgments.get(entry["question"], {}).items()
                      if key.startswith(f"{kind}:")}
            if not any(g >= 1 for g in grades.values()):
                continue
            for variant, ranking in entry["rankings"].items():
                out.setdefault((kind, variant), {})[entry["question"]] = score_ranking(ranking[kind], grades, cutoffs)
    return out


def compute_metrics(run_dir: Path, judge: str | None = None, baseline: str | None = None,
                    cutoffs: tuple[int, ...] | None = None) -> list[dict[str, Any]]:
    """Mean metrics per kind and variant; the sign test runs on nDCG at the largest cutoff."""
    pool = load_pool(run_dir)
    cutoffs = cutoffs_for(pool, cutoffs)
    scores = per_question(pool, load_judgments(run_dir, judge), cutoffs)
    main = f"nDCG@{max(cutoffs)}"
    rows = []
    for (kind, variant), by_question in sorted(scores.items()):
        values = list(by_question.values())
        row: dict[str, Any] = {"kind": kind, "variant": variant, "questions": len(values)}
        for metric in values[0]:
            row[metric] = round(sum(v[metric] for v in values) / len(values), 3)
        if baseline and variant != baseline and (kind, baseline) in scores:
            row.update(sign_test(scores[(kind, baseline)], by_question, main))
        rows.append(row)
    if rows:
        name = "metrics.csv" if judge is None else f"metrics_{judge}.csv"
        columns = list(dict.fromkeys(key for row in rows for key in row))
        with (run_dir / name).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    return rows


def sign_test(baseline: dict[str, dict], variant: dict[str, dict], metric: str) -> dict[str, Any]:
    """Wins and losses of a variant against the baseline, per question, and the two-sided p.

    The sign test assumes nothing about how the differences are spread,
    which suits a few dozen questions; ties are left out, as usual.
    """
    common = baseline.keys() & variant.keys()
    diffs = [variant[q][metric] - baseline[q][metric] for q in common]
    wins = sum(d > 1e-9 for d in diffs)
    losses = sum(d < -1e-9 for d in diffs)
    n = wins + losses
    tail = sum(math.comb(n, i) for i in range(0, min(wins, losses) + 1)) / 2 ** n if n else 1.0
    return {"delta": round(sum(diffs) / len(diffs), 3) if diffs else 0.0,
            "wins": wins, "losses": losses, "p": round(min(1.0, 2 * tail), 4)}


# --- answerability ------------------------------------------------------------------

# Questions of this type ask about topics the graph does not cover. Ranking
# metrics skip them (nothing is relevant, so nDCG is undefined); what they
# test is whether the system answers empty - see `abstention`.
NO_ANSWER_TYPE = "no_answer"


def calibrate(rag: Rag, questions_path: Path, variant: str, quantile: float = 0.95) -> dict[str, Any]:
    """A `min_score` for the variant's model, from questions on topics the graph does not cover.

    The threshold is the `quantile` of their best raw matches: about
    1 - quantile of such questions still get an answer. The validation
    questions are not used, so the empty answers measured on them stay an
    honest estimate.
    """
    spec = parse_variant(variant)
    scores = []
    for meta in read_questions(questions_path):
        question = meta["question"]
        label = "Repository" if targets(meta) == ("repositories",) else "Publication"
        bm25_query = rag.extract_keywords(question) if spec["bm25_keywords"] and spec["mode"] != "dense" else None
        dense_query = rag.extract_keywords(question) if spec["dense_keywords"] and spec["mode"] != "bm25" else None
        hits = rag.retrieve(question, profile=spec["profile"], model=spec["model"], mode=spec["mode"],
                            fusion=spec["fusion"], bm25_query=bm25_query, dense_query=dense_query,
                            labels=(label,)).get(label, [])
        dense = [h.dense for h in hits if h.dense is not None]
        if dense:
            scores.append({"question": question, "label": label, "top_dense": max(dense)})
    values = sorted(s["top_dense"] for s in scores)
    threshold = float(np.quantile(values, quantile))
    return {"variant": variant, "model": spec["model"], "questions": len(values), "quantile": quantile,
            "min_score": round(threshold, 4), "range": [round(values[0], 4), round(values[-1], 4)],
            "above": sorted((s for s in scores if s["top_dense"] >= threshold), key=lambda s: -s["top_dense"])}


def abstention(pool: dict[str, Any]) -> list[dict[str, Any]]:
    """Empty answers per variant: on unanswerable questions (right) and on the others (wrong).

    A question is answered empty when the best raw match of the label it
    targets falls below the model's `min_score`, as `Rag.ask` does. AUC and
    the threshold maximising right minus wrong are given for every variant
    with dense scores, so a model without `min_score` can be calibrated.
    """
    rows = []
    for name, spec in pool["variants"].items():
        scored = []
        for entry in pool["questions"]:
            label = "Repository" if targets(entry["meta"]) == ("repositories",) else "Publication"
            top = entry["rankings"][name].get("top_dense", {}).get(label)
            if top is not None:
                scored.append((entry["meta"].get("type") == NO_ANSWER_TYPE, top))
        unanswerable = [top for no_answer, top in scored if no_answer]
        answerable = [top for no_answer, top in scored if not no_answer]
        if not unanswerable or not answerable:
            continue
        threshold = get_model(spec["model"]).min_score
        row = {"variant": name, "unanswerable": len(unanswerable), "answerable": len(answerable),
               "auc": _auc(answerable, unanswerable), "min_score": threshold}
        if threshold is not None:
            row["empty_right"] = sum(top < threshold for top in unanswerable)
            row["empty_wrong"] = sum(top < threshold for top in answerable)
        best = _best_threshold(answerable, unanswerable)
        row["best_threshold"] = best["threshold"]
        rows.append(row)
    return rows


def _auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 3)


def _best_threshold(pos: list[float], neg: list[float]) -> dict[str, Any]:
    """The cut (answer if score >= cut) maximising TPR - FPR (Youden's J)."""
    if not pos or not neg:
        return {}
    best = None
    for cut in sorted(set(pos + neg)):
        tpr = sum(p >= cut for p in pos) / len(pos)
        fpr = sum(n >= cut for n in neg) / len(neg)
        if best is None or tpr - fpr > best[0]:
            best = (tpr - fpr, cut, tpr, fpr)
    return {"threshold": round(best[1], 4), "answered": round(best[2], 3), "false_answers": round(best[3], 3)}


def agreement(run_dir: Path, judge: str, other: str | None = None,
               resamples: int = 1000) -> list[dict[str, Any]]:
    """Exact share and Cohen's kappa (plain and weighted) on the items both judges graded.

    The 95% interval of the weighted kappa resamples whole questions, not
    items: the items of one question share a topic and tend to be judged
    alike, so treating them as independent would make the interval too narrow.
    """
    a, b = load_judgments(run_dir, other), load_judgments(run_dir, judge)
    rng = np.random.default_rng(0)
    rows = []
    for kind in [*KINDS, "all"]:
        by_question = [[(grades[key], b[question][key]) for key in grades
                        if (kind == "all" or key.startswith(f"{kind}:")) and key in b.get(question, {})]
                       for question, grades in a.items()]
        by_question = [pairs for pairs in by_question if pairs]
        pairs = [pair for group in by_question for pair in group]
        if not pairs:
            continue
        row = {"kind": kind, "items": len(pairs), "questions": len(by_question),
               "exact": round(sum(x == y for x, y in pairs) / len(pairs), 3),
               "kappa": round(kappa(pairs, weighted=False), 3),
               "weighted_kappa": round(kappa(pairs, weighted=True), 3)}
        if len(by_question) > 1:
            draws = [kappa([p for n in rng.integers(len(by_question), size=len(by_question))
                            for p in by_question[n]], weighted=True) for _ in range(resamples)]
            row["weighted_kappa_ci"] = [round(float(q), 3) for q in np.quantile(draws, [0.025, 0.975])]
        rows.append(row)
    return rows


def kappa(pairs: list[tuple[int, int]], weighted: bool) -> float:
    n = len(pairs)
    span = max(GRADES) - min(GRADES)

    def disagreement(x: int, y: int) -> float:
        return abs(x - y) / span if weighted else float(x != y)

    observed = sum(disagreement(x, y) for x, y in pairs) / n
    left, right = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    expected = sum(left[x] * right[y] * disagreement(x, y) for x in GRADES for y in GRADES) / n ** 2
    return 1.0 if expected == 0 else 1 - observed / expected


def _write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
