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
from pathlib import Path
from typing import Any

from .readme import clean_readme, load_readme
from .retrieve import MODES, Hit, Rag

K = 10
# The model of a variant written without `@model`; see parse_variant.
VARIANT_DEFAULT_MODEL = "bge"
KINDS = ("persons", "publications", "repositories")
GRADES = (0, 1, 2)
SNIPPET_CHARS = 700
_JUDGE = re.compile(r"[a-z0-9_]{1,32}")

PUBLICATIONS_FOR_JUDGE = """
UNWIND $ids AS id MATCH (p:Publication {id: id})
RETURN p.id AS id, p.title AS title, p.year AS year, p.journal AS journal, p.abstract AS abstract,
       p.doi AS doi, p.openalex_url AS openalex_url,
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
            }
            for p in people:
                seen = persons.setdefault(p["id"], {"label": p["label"], "url": p["url"], "is_itmo": p["is_itmo"],
                                                    "departments": p["departments"], "evidence": [], "code": []})
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
        # A person is judged by what they wrote: show the titles behind the score.
        titles = {i: d["title"] for i, d in items["publications"].items()}
        missing = {i for p in persons.values() for i in p["evidence"]} - titles.keys()
        titles.update({r["id"]: r["title"] for r in rag.graph.read(PUBLICATIONS_FOR_JUDGE, ids=sorted(missing))})
        code_ids = sorted({i for p in persons.values() for i in p["code"]})
        names = {r["id"]: r["name"] for r in rag.graph.read(REPOSITORIES_FOR_JUDGE, ids=code_ids)}
        for p in persons.values():
            p["evidence"] = [titles.get(i) or i for i in p["evidence"][:5]]
            p["code"] = [names.get(i) or i for i in p["code"][:5]]
        entries.append({"question": question, "meta": meta, "rankings": rankings, "items": items,
                        "order": {kind: _shuffled(question, list(items[kind])) for kind in KINDS}})
    pool = {"k": k, "variants": specs, "kinds": list(KINDS), "questions": entries}
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "pool.json", pool)
    return pool


def _item_details(rag: Rag, rankings: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, dict[str, Any]]]:
    pub_ids = sorted({i for r in rankings.values() for i in r["publications"]})
    repo_ids = sorted({i for r in rankings.values() for i in r["repositories"]})
    publications = {}
    for row in rag.graph.read(PUBLICATIONS_FOR_JUDGE, ids=pub_ids):
        authors = [a["name"] for a in sorted(row["authors"], key=lambda a: a["position"] or 0)]
        doi = row.get("doi")
        publications[row["id"]] = {
            "title": row["title"], "year": row["year"], "journal": row["journal"],
            "url": (doi if doi and doi.startswith("http") else f"https://doi.org/{doi}") if doi else row["openalex_url"],
            "authors": authors[:8] + (["…"] if len(authors) > 8 else []), "abstract": _snippet(row["abstract"]),
        }
    repositories = {}
    for row in rag.graph.read(REPOSITORIES_FOR_JUDGE, ids=repo_ids):
        repositories[row["id"]] = {
            "title": row["name"], "url": row["url"], "description": row["description"], "stars": row["stars"],
            "implemented": row["implemented"][:3],
            "readme": _snippet(clean_readme(load_readme(rag.config, row["id"]) or "")),
        }
    return {"publications": publications, "repositories": repositories}


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


def agreement(run_dir: Path, judge: str, other: str | None = None) -> list[dict[str, Any]]:
    """Exact share and Cohen's kappa (plain and weighted) on the items both judges graded."""
    a, b = load_judgments(run_dir, other), load_judgments(run_dir, judge)
    rows = []
    for kind in KINDS:
        pairs = [(grades[key], b[question][key])
                 for question, grades in a.items() for key in grades
                 if key.startswith(f"{kind}:") and key in b.get(question, {})]
        if pairs:
            rows.append({"kind": kind, "items": len(pairs),
                         "exact": round(sum(x == y for x, y in pairs) / len(pairs), 3),
                         "kappa": round(kappa(pairs, weighted=False), 3),
                         "weighted_kappa": round(kappa(pairs, weighted=True), 3)})
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
