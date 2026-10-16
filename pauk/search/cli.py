"""`pauk search ...` - build the search index and ask questions from the terminal."""

from __future__ import annotations

import json

from pauk.settings import Settings

from .retrieval import MODES


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("search", help="free-text search over the graph")
    sub = parser.add_subparsers(dest="search_command", required=True)

    p = sub.add_parser("fetch-readmes", help="download repository READMEs from GitHub into data/search")
    p.add_argument("--force", action="store_true", help="re-download READMEs already on disk")

    sub.add_parser("build-index", help="read the graph (read-only) and build embeddings and name indexes")

    p = sub.add_parser("ask", help="answer one question and print the result as JSON")
    p.add_argument("question")
    p.add_argument("--mode", choices=MODES, default="hybrid")
    p.add_argument("--ranking", choices=["graph", "authorship"], default="graph")
    p.add_argument("--no-llm-parse", action="store_true", help="skip LLM №1, search the whole question as a topic")
    p.add_argument("--no-llm-answer", action="store_true", help="skip LLM №2, return the ranked sections only")
    p.add_argument("--top", type=int, default=10)

    p = sub.add_parser("batch", help="answer a list of questions and write results.jsonl + review.csv")
    p.add_argument("questions", help=".txt (one question per line) or .csv with a 'question' column")
    p.add_argument("--out", help="output directory (default: <data_dir>/search/batch)")
    p.add_argument("--mode", choices=MODES, default="hybrid")
    p.add_argument("--ranking", choices=["graph", "authorship"], default="graph")
    p.add_argument("--no-llm-parse", action="store_true", help="skip LLM №1 (no tokens; every question is a topic)")
    p.add_argument("--no-llm-answer", action="store_true", help="skip LLM №2, keep the ranked sections only")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--force", action="store_true", help="re-ask questions already answered with the same options")

    p = sub.add_parser("pool", help="run every search variant on a batch run's questions and pool their top-k")
    p.add_argument("--run", default="batch", help="batch run directory under <data_dir>/search (default: batch)")
    p.add_argument("--k", type=int, default=10, help="pool depth per variant (default: 10)")

    p = sub.add_parser("metrics", help="P@k, nDCG@k and MRR per variant from the judged pool")
    p.add_argument("--run", default="batch", help="batch run directory under <data_dir>/search (default: batch)")
    p.add_argument("--judge", help="use judgments_<judge>.json instead of judgments.json, and report "
                                    "how far that judge agrees with judgments.json")


def run(args, config: Settings) -> None:
    if args.search_command == "fetch-readmes":
        from .readme import fetch_all

        print(json.dumps(fetch_all(config, force=args.force), ensure_ascii=False))
    elif args.search_command == "build-index":
        from .index import build

        print(json.dumps(build(config), ensure_ascii=False, indent=2))
    elif args.search_command in ("pool", "metrics"):
        from .batch import resolve_run
        from .evaluation import build_pool, compute_metrics, load_judgments, progress

        run_dir = resolve_run(config.search_dir, args.run)
        if args.search_command == "pool":
            from .pipeline import Engine

            engine = Engine(config)
            try:
                pool = build_pool(run_dir, engine, k=args.k)
            finally:
                engine.close()
            print(json.dumps({"pool": str(run_dir / "pool.json"),
                              "questions": progress(pool, load_judgments(run_dir)),
                              "skipped": [q["question"] for q in pool["questions"] if q.get("skipped")],
                              "judge at": "http://localhost:8501/review (Пул)"}, ensure_ascii=False, indent=2))
        else:
            from .evaluation import agreement

            rows = compute_metrics(run_dir, args.judge)
            if not rows:
                print("No judgments yet: grade the pool on http://localhost:8501/review first.")
            for row in rows:
                print("  ".join(f"{key}={value}" for key, value in row.items()))
            if args.judge:
                print(f"\nagreement of {args.judge} with judgments.json on items both graded:")
                for row in agreement(run_dir, args.judge) or [{"items": 0}]:
                    print("  ".join(f"{key}={value}" for key, value in row.items()))
    elif args.search_command == "batch":
        from pathlib import Path

        from .batch import BROKEN_STEPS
        from .batch import run as run_batch
        from .pipeline import AskOptions

        out = Path(args.out) if args.out else config.search_dir / "batch"
        summary = run_batch(Path(args.questions), out, AskOptions(
            retrieval_mode=args.mode, ranking=args.ranking, llm_parse=not args.no_llm_parse,
            llm_answer=not args.no_llm_answer, top_n=args.top,
        ), force=args.force, config=config)
        summary["broken_step values"] = BROKEN_STEPS
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        from .pipeline import AskOptions, Engine

        engine = Engine(config)
        try:
            result = engine.ask(args.question, AskOptions(
                retrieval_mode=args.mode, ranking=args.ranking, llm_parse=not args.no_llm_parse,
                llm_answer=not args.no_llm_answer, top_n=args.top,
            ))
        finally:
            engine.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
