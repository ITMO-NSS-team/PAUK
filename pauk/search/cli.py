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


def run(args, config: Settings) -> None:
    if args.search_command == "fetch-readmes":
        from .readme import fetch_all

        print(json.dumps(fetch_all(config, force=args.force), ensure_ascii=False))
    elif args.search_command == "build-index":
        from .index import build

        print(json.dumps(build(config), ensure_ascii=False, indent=2))
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
