"""`pauk rag ...`: READMEs, index profiles, questions and validation."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pauk.settings import Settings

from .models import MODELS
from .profiles import FIELDS, PRESETS, resolve
from .retrieve import MODES

DEFAULT_VARIANTS = "full:hybrid:en,full:hybrid,full:dense,full:bm25"

logger = logging.getLogger(__name__)


def _csv(value: str | None) -> list[str] | None:
    return None if value is None else [part.strip() for part in value.split(",") if part.strip()]


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("rag", help="retrieval-augmented search: indexes in Neo4j, ask, validate")
    sub = parser.add_subparsers(dest="rag_command", required=True)

    p = sub.add_parser("readmes", help="cache repository READMEs from GitHub under data/rag/readmes")
    p.add_argument("--force", action="store_true", help="download again even when cached")
    p.add_argument("--seed", action="append", default=[], help="an earlier README cache to copy from first")

    p = sub.add_parser("profiles", help="presets, fields, and what is built in Neo4j")

    p = sub.add_parser("index", help="build (or update) one index profile in Neo4j")
    p.add_argument("--model", choices=list(MODELS), default=None,
                   help="embedding model alias (default: PAUK_RAG_EMBEDDING_MODEL)")
    p.add_argument("--profile", required=True, help=f"a preset ({', '.join(PRESETS)}) or a new name with fields")
    p.add_argument("--publication-fields", help=f"comma-separated: {', '.join(FIELDS['Publication'])}")
    p.add_argument("--repository-fields", help=f"comma-separated: {', '.join(FIELDS['Repository'])}")
    p.add_argument("--force", action="store_true", help="re-embed every node, not only changed texts")
    p.add_argument("--fetch-readmes", action="store_true", help="download missing READMEs first")

    p = sub.add_parser("drop", help="remove a profile's properties and indexes from Neo4j")
    p.add_argument("--profile", required=True)
    p.add_argument("--model", choices=list(MODELS), default=None,
                   help="drop only this model's vectors; without it the whole profile goes")

    p = sub.add_parser("ask", help="answer one question, print JSON")
    p.add_argument("question")
    p.add_argument("--model", choices=list(MODELS), default=None,
                   help="embedding model alias (default: PAUK_RAG_EMBEDDING_MODEL)")
    p.add_argument("--profile", default="full")
    p.add_argument("--mode", choices=MODES, default="hybrid")
    p.add_argument("--fusion", choices=["minmax", "rrf"], default="minmax")
    p.add_argument("--no-keywords", action="store_true",
                   help="search by the question as written, without LLM keyword extraction (saves one LLM call)")
    p.add_argument("--keywords", default=None,
                   help="use these English keywords instead of extracting them (as the planner passes them)")
    p.add_argument("--no-abstain", action="store_true",
                   help="always show the nearest results, even when the best match is below the model's threshold")
    p.add_argument("--no-dense-keywords", action="store_true",
                   help="give the encoder the question as written instead of the extracted keywords "
                        "(BM25 still gets them)")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--all-people", action="store_true", help="keep co-authors from outside ITMO")
    p.add_argument("--no-answer", action="store_true", help="retrieval only, no LLM call")

    p = sub.add_parser("pool", help="run the variants on the questions and pool their top-k for judging")
    p.add_argument("--run", default="validation", help="run directory under data/rag/runs")
    p.add_argument("--run-dir", default=None, help="run directory anywhere, instead of --run under data/rag/runs")
    p.add_argument("--questions", default=None, help="CSV with a question column (default: <run>/questions.csv)")
    p.add_argument("--groups", default="3",
                   help="comma-separated question groups to pool, from ids like q152t2 (default: 3, the semantic "
                        "questions; rows without such an id are always kept)")
    p.add_argument("--keywords-file", default=None,
                   help="hand-kept keywords by question id (default: data/rag/keywords.json)")
    # No default list: rerunning `pool` on a judged run must not silently
    # shrink it to a few variants and drop the others from the metrics.
    p.add_argument("--variants", default=None,
                   help="comma-separated profile[@model]:mode[:rrf+en]; default: the run's current variants, "
                        f"or {DEFAULT_VARIANTS} for a new run")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--seed-judgments", action="append", default=[],
                   help="judgments JSON of an earlier run to copy matching grades from")
    p.add_argument("--judge", help="seed into judgments_<judge>.json instead of judgments.json")

    p = sub.add_parser("calibrate", help="a model's min_score from off-topic questions, for empty answers")
    p.add_argument("--variant", default="full@qwen3:hybrid:en+kw",
                   help="the configuration ask uses; the threshold holds only for its query")
    p.add_argument("--questions", default=None,
                   help="CSV of questions the graph does not cover (default: data/rag/calibration/offtopic.csv)")
    p.add_argument("--quantile", type=float, default=0.95,
                   help="share of off-topic questions to answer empty (default 0.95)")

    p = sub.add_parser("judge", help="grade a run's publications and repositories with an OpenRouter model")
    p.add_argument("--run", default="human_check")
    p.add_argument("--run-dir", default=None, help="run directory anywhere, instead of --run under data/rag/runs")
    p.add_argument("--judge", required=True, help="writes judgments_<judge>.json, e.g. qwen")
    p.add_argument("--model", default=None, help="OpenRouter model id (default: PAUK_LLM_MODEL)")
    p.add_argument("--against", default=None,
                   help="judge to report agreement with when done (default: judgments.json, the manual grades)")
    p.add_argument("--workers", type=int, default=8, help="parallel requests (one item each)")
    p.add_argument("--reasoning-effort", default=None,
                   help="for reasoning models, e.g. minimal: OpenRouter's reasoning effort")
    p.add_argument("--limit", type=int, default=None, help="judge only this many items (a probe)")
    p.add_argument("--no-depth-line", action="store_true",
                   help="the prompt without the pool-depth line, as the tails run was judged")

    p = sub.add_parser("tails", help="rank of the last relevant item per variant, to choose the pool depth")
    p.add_argument("--run", default="tails")
    p.add_argument("--run-dir", default=None, help="run directory anywhere, instead of --run under data/rag/runs")
    p.add_argument("--judge", required=True)

    p = sub.add_parser("metrics", help="P@k, R@k, nDCG@k, MRR per variant; sign test against a baseline")
    p.add_argument("--run", default="validation")
    p.add_argument("--run-dir", default=None, help="run directory anywhere, instead of --run under data/rag/runs")
    p.add_argument("--k", help="comma-separated cutoffs, e.g. 3,5,10 (default: 5 and the pool's k; "
                               "none above the pool's k); the sign test uses nDCG at the largest")
    p.add_argument("--judge", help="use judgments_<judge>.json and report agreement with judgments.json")
    p.add_argument("--baseline", help="variant the others are compared with, e.g. full:hybrid")
    p.add_argument("--against", default=None, help="judge to compare --judge with instead of judgments.json")


def runs_dir(config: Settings) -> Path:
    return config.data_dir / "rag" / "runs"


def _run_dir(config: Settings, args) -> Path:
    return Path(args.run_dir) if args.run_dir else runs_dir(config) / args.run


def run(args, config: Settings) -> None:
    command = args.rag_command
    if command == "readmes":
        from .graph import Graph
        from .readme import fetch

        graph = Graph(config)
        try:
            repos = graph.read("MATCH (r:Repository) WHERE r.has_readme RETURN r.id AS id, r.url AS url ORDER BY r.id")
        finally:
            graph.close()
        print(json.dumps(fetch(config, repos, force=args.force, seed_dirs=tuple(Path(s) for s in args.seed)),
                         indent=2))
    elif command == "profiles":
        from .graph import Graph

        graph = Graph(config)
        try:
            indexes = graph.rag_indexes()
        finally:
            graph.close()
        # Manifests are named profile@model, one per build.
        manifests = sorted((config.data_dir / "rag" / "profiles").glob("*@*.json"))
        built = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in manifests}
        print(json.dumps({
            "presets": {name: p.to_dict() for name, p in PRESETS.items()},
            "fields": {label: sorted(names) for label, names in FIELDS.items()},
            "models": {alias: model.name for alias, model in MODELS.items()},
            "built": {key: {"embedding_model": m.get("embedding_model"), "built_at": m.get("built_at"),
                            "labels": m.get("labels")} for key, m in built.items()},
            "indexes": indexes,
        }, ensure_ascii=False, indent=2, default=str))
    elif command == "index":
        from .index import build

        profile = resolve(args.profile, _csv(args.publication_fields), _csv(args.repository_fields))
        if args.fetch_readmes and "Repository" in profile.labels():
            from .graph import Graph
            from .readme import fetch

            graph = Graph(config)
            try:
                repos = graph.read("MATCH (r:Repository) WHERE r.has_readme RETURN r.id AS id, r.url AS url")
            finally:
                graph.close()
            logger.info("READMEs: %s", fetch(config, repos))
        print(json.dumps(build(config, profile, force=args.force, model=args.model), ensure_ascii=False, indent=2))
    elif command == "drop":
        from .graph import Graph
        from .index import manifest_path
        from .profiles import LABELS, Profile

        graph = Graph(config)
        try:
            # Any name: dropping needs only the naming scheme, not the fields.
            removed = graph.drop(Profile(args.profile, publication=("title",)), LABELS, args.model)
        finally:
            graph.close()
        for model in [args.model] if args.model else MODELS:
            manifest_path(config, args.profile, model).unlink(missing_ok=True)
        print(json.dumps({"profile": args.profile, "model": args.model, "cleared_nodes": removed}, indent=2))
    elif command == "ask":
        from .retrieve import Rag

        rag = Rag(config, model=args.model)
        try:
            result = rag.ask(args.question, profile=args.profile, mode=args.mode, fusion=args.fusion,
                             keywords=not args.no_keywords, dense_keywords=not (args.no_keywords or args.no_dense_keywords),
                             query_keywords=args.keywords, abstain=not args.no_abstain,
                             top_n=args.top, only_itmo=not args.all_people,
                             with_answer=not args.no_answer)
        finally:
            rag.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif command == "pool":
        from .evaluation import build_pool, seed_judgments
        from .retrieve import Rag

        run_dir = _run_dir(config, args)
        questions = Path(args.questions) if args.questions else run_dir / "questions.csv"
        if args.variants is not None:
            # An empty list (an unset shell variable) must fail, not fall back
            # to the defaults and judge a pool nobody asked for.
            variants = _csv(args.variants)
            if not variants:
                raise SystemExit("--variants is empty")
        elif (run_dir / "pool.json").is_file():
            variants = list(json.loads((run_dir / "pool.json").read_text(encoding="utf-8"))["variants"])
        else:
            variants = _csv(DEFAULT_VARIANTS)
        groups = tuple(_csv(args.groups))
        if not groups:
            raise SystemExit("--groups is empty")
        rag = Rag(config)
        try:
            pool = build_pool(run_dir, rag, questions, variants, args.k, groups=groups,
                              keywords_path=Path(args.keywords_file) if args.keywords_file else None)
        finally:
            rag.close()
        seeded = {source: seed_judgments(run_dir, Path(source), args.judge) for source in args.seed_judgments}
        items = sum(len(v) for q in pool["questions"] for v in q["items"].values())
        print(json.dumps({"run": str(run_dir), "questions": len(pool["questions"]), "items": items,
                          "variants": list(pool["variants"]), "seeded": seeded}, ensure_ascii=False, indent=2))
    elif command == "calibrate":
        from .evaluation import calibrate
        from .retrieve import Rag

        questions = Path(args.questions) if args.questions else config.data_dir / "rag" / "calibration" / "offtopic.csv"
        rag = Rag(config)
        try:
            print(json.dumps(calibrate(rag, questions, args.variant, args.quantile), ensure_ascii=False, indent=2))
        finally:
            rag.close()
    elif command == "judge":
        from pauk.sources.llm import OpenRouterClient

        from .evaluation import (
            _write_json,
            agreement,
            judge_pool,
            judgments_path,
            load_judgments,
            load_pool,
            person_grades,
        )

        run_dir = _run_dir(config, args)

        def make_llm():
            return OpenRouterClient(config.request_timeout, config.openrouter_api_key,
                                    args.model or config.llm_model, config.openrouter_proxy_url)

        result = judge_pool(run_dir, make_llm, args.judge, args.workers, state_depth=not args.no_depth_line,
                            reasoning_effort=args.reasoning_effort, limit=args.limit)
        # People are graded from their works by rule, never by the model.
        judgments = load_judgments(run_dir, args.judge)
        result["persons"] = person_grades(load_pool(run_dir), judgments)
        _write_json(judgments_path(run_dir, args.judge), judgments)
        print(json.dumps({"model": args.model or config.llm_model, **result,
                          "agreement": agreement(run_dir, args.judge, args.against or None)},
                         ensure_ascii=False, indent=2))
    elif command == "tails":
        import csv

        from .evaluation import _write_json, load_judgments, load_pool, tails

        run_dir = _run_dir(config, args)
        rows = tails(load_pool(run_dir), load_judgments(run_dir, args.judge))
        with (run_dir / f"tails_{args.judge}.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["question"])
            writer.writeheader()
            writer.writerows(rows)
        summary = {}
        for kind in ("publications", "repositories"):
            mine = [r for r in rows if r["kind"] == kind]
            if mine:
                deepest = max(mine, key=lambda r: r["last_relevant"])
                summary[kind] = {"max_last_relevant": deepest["last_relevant"], "depth": deepest["depth"],
                                 "at": {"question": deepest["question"], "variant": deepest["variant"]},
                                 "cut_by_ceiling": sum(r["last_relevant"] == r["depth"] for r in mine),
                                 "unjudged": sum(r["unjudged"] for r in mine)}
        _write_json(run_dir / f"tails_{args.judge}.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif command == "metrics":
        from .evaluation import abstention, agreement, agreement_by_question, compute_metrics, load_pool

        run_dir = _run_dir(config, args)
        cutoffs = tuple(int(k) for k in _csv(args.k)) if args.k else None
        rows = compute_metrics(run_dir, args.judge, args.baseline, cutoffs)
        columns = list(dict.fromkeys(key for row in rows for key in row))
        print("\t".join(columns))
        for row in rows:
            print("\t".join(str(row.get(c, "")) for c in columns))
        for row in abstention(load_pool(run_dir)):
            right = f"right {row['empty_right']}/{row['unanswerable']}, wrong {row['empty_wrong']}/{row['answerable']}" \
                if row["min_score"] is not None else "no threshold for this model"
            print(f"empty answers, {row['variant']}: {right}; auc {row['auc']}, best threshold {row['best_threshold']}")
        if args.judge:
            against = f"judgments_{args.against}.json" if args.against else "judgments.json"
            for row in agreement(run_dir, args.judge, args.against):
                print(f"agreement with {against}, {row['kind']}: {row['items']} items, exact {row['exact']}, "
                      f"kappa {row['kappa']}, weighted {row['weighted_kappa']} {row.get('weighted_kappa_ci', '')}")
            for row in agreement_by_question(run_dir, args.judge, args.against):
                print(f"agreement with {against}, question: {row['items']} items, exact {row['exact']}, "
                      f"weighted {row['weighted_kappa']} - {row['question']}")
