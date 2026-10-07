"""`pauk rag ...`: READMEs, index profiles, questions and validation."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pauk.settings import Settings

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
    p.add_argument("--profile", required=True, help=f"a preset ({', '.join(PRESETS)}) or a new name with fields")
    p.add_argument("--publication-fields", help=f"comma-separated: {', '.join(FIELDS['Publication'])}")
    p.add_argument("--repository-fields", help=f"comma-separated: {', '.join(FIELDS['Repository'])}")
    p.add_argument("--force", action="store_true", help="re-embed every node, not only changed texts")
    p.add_argument("--fetch-readmes", action="store_true", help="download missing READMEs first")

    p = sub.add_parser("drop", help="remove a profile's properties and indexes from Neo4j")
    p.add_argument("--profile", required=True)

    p = sub.add_parser("ask", help="answer one question, print JSON")
    p.add_argument("question")
    p.add_argument("--profile", default="full")
    p.add_argument("--mode", choices=MODES, default="hybrid")
    p.add_argument("--fusion", choices=["minmax", "rrf"], default="minmax")
    p.add_argument("--no-translate", action="store_true",
                   help="BM25 on the question as written, not on its English keywords (saves one LLM call)")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--all-people", action="store_true", help="keep co-authors from outside ITMO")
    p.add_argument("--no-answer", action="store_true", help="retrieval only, no LLM call")

    p = sub.add_parser("pool", help="run the variants on the questions and pool their top-k for judging")
    p.add_argument("--run", default="validation", help="run directory under data/rag/runs")
    p.add_argument("--questions", default=None, help="CSV with a question column (default: <run>/questions.csv)")
    # No default list: rerunning `pool` on a judged run must not silently
    # shrink it to a few variants and drop the others from the metrics.
    p.add_argument("--variants", default=None,
                   help="comma-separated profile:mode[:rrf+en]; default: the run's current variants, "
                        f"or {DEFAULT_VARIANTS} for a new run")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--seed-judgments", action="append", default=[],
                   help="judgments JSON of an earlier run to copy matching grades from")
    p.add_argument("--judge", help="seed into judgments_<judge>.json instead of judgments.json")

    p = sub.add_parser("metrics", help="P@k, nDCG@k, MRR per variant; sign test against a baseline")
    p.add_argument("--run", default="validation")
    p.add_argument("--judge", help="use judgments_<judge>.json and report agreement with judgments.json")
    p.add_argument("--baseline", help="variant the others are compared with, e.g. full:hybrid")


def runs_dir(config: Settings) -> Path:
    return config.data_dir / "rag" / "runs"


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
        from .index import load_manifest

        graph = Graph(config)
        try:
            indexes = graph.rag_indexes()
        finally:
            graph.close()
        built = sorted({row["name"].split("_", 2)[2].rsplit("_", 1)[0] for row in indexes})
        print(json.dumps({
            "presets": {name: p.to_dict() for name, p in PRESETS.items()},
            "fields": {label: sorted(names) for label, names in FIELDS.items()},
            "built": {name: (load_manifest(config, name) or {}).get("labels") for name in built},
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
        print(json.dumps(build(config, profile, force=args.force), ensure_ascii=False, indent=2))
    elif command == "drop":
        from .graph import Graph
        from .index import manifest_path
        from .profiles import LABELS, Profile

        graph = Graph(config)
        try:
            # Any name: dropping needs only the naming scheme, not the fields.
            removed = graph.drop(Profile(args.profile, publication=("title",)), LABELS)
        finally:
            graph.close()
        manifest_path(config, args.profile).unlink(missing_ok=True)
        print(json.dumps({"profile": args.profile, "cleared_nodes": removed}, indent=2))
    elif command == "ask":
        from .retrieve import Rag

        rag = Rag(config)
        try:
            result = rag.ask(args.question, profile=args.profile, mode=args.mode, fusion=args.fusion,
                             translate=not args.no_translate, top_n=args.top, only_itmo=not args.all_people,
                             with_answer=not args.no_answer)
        finally:
            rag.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif command == "pool":
        from .evaluation import build_pool, seed_judgments
        from .retrieve import Rag

        run_dir = runs_dir(config) / args.run
        questions = Path(args.questions) if args.questions else run_dir / "questions.csv"
        if args.variants:
            variants = _csv(args.variants)
        elif (run_dir / "pool.json").is_file():
            variants = list(json.loads((run_dir / "pool.json").read_text(encoding="utf-8"))["variants"])
        else:
            variants = _csv(DEFAULT_VARIANTS)
        rag = Rag(config)
        try:
            pool = build_pool(run_dir, rag, questions, variants, args.k)
        finally:
            rag.close()
        seeded = {source: seed_judgments(run_dir, Path(source), args.judge) for source in args.seed_judgments}
        items = sum(len(v) for q in pool["questions"] for v in q["items"].values())
        print(json.dumps({"run": str(run_dir), "questions": len(pool["questions"]), "items": items,
                          "variants": list(pool["variants"]), "seeded": seeded}, ensure_ascii=False, indent=2))
    elif command == "metrics":
        from .evaluation import agreement, compute_metrics

        run_dir = runs_dir(config) / args.run
        rows = compute_metrics(run_dir, args.judge, args.baseline)
        columns = list(dict.fromkeys(key for row in rows for key in row))
        print("\t".join(columns))
        for row in rows:
            print("\t".join(str(row.get(c, "")) for c in columns))
        if args.judge:
            for row in agreement(run_dir, args.judge):
                print(f"agreement with judgments.json, {row['kind']}: {row['items']} items, exact {row['exact']}, "
                      f"kappa {row['kappa']}, weighted {row['weighted_kappa']}")
