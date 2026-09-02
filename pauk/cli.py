from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pauk.logging import configure_logging
from pauk.pipeline.collect import Collector
from pauk.pipeline.enrich import Enricher
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.runner import PipelineRunner
from pauk.pipeline.selectors import PeriodSelector, WorkSelector, WorksFileSelector
from pauk.pipeline.stages import ALL_STAGES, OPTIONAL_STAGES
from pauk.pipeline.stages.base import PreparedSelection
from pauk.settings import settings
from pauk.sources import OpenAlexClient
from pauk.storage import PreparedStore, RawStore, ensure_indexes, get_mongo_client
from pauk.storage.naming import group_name, validate_group


def _selector(args):
    if args.work:
        return WorkSelector(args.work)
    if args.works_file:
        return WorksFileSelector(Path(args.works_file))
    if args.date_from and args.date_to:
        return PeriodSelector(args.date_from, args.date_to)
    raise SystemExit("provide --work, --works-file, or both --from and --to")


def _group(args) -> str:
    selector = _selector(args)
    return validate_group(group_name(
        work_id=selector.work_id if isinstance(selector, WorkSelector) else None,
        date_from=selector.date_from if isinstance(selector, PeriodSelector) else None,
        date_to=selector.date_to if isinstance(selector, PeriodSelector) else None,
        name=args.name,
    ))


def _selection_from_input(path: str, entity: str) -> PreparedSelection:
    ids = frozenset(
        line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
    )
    return PreparedSelection(entity, ids)


logger = logging.getLogger("pauk.cli")


def _log_result(action: str, group: str | None, result: dict) -> None:
    summary = ", ".join(f"{key}={value}" for key, value in result.items()) or "nothing to do"
    logger.info("%s: %s", f"{action} {group}" if group else action, summary)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pauk")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "collect"):
        p = sub.add_parser(name)
        p.add_argument("--work")
        p.add_argument("--works-file")
        p.add_argument("--from", dest="date_from")
        p.add_argument("--to", dest="date_to")
        p.add_argument("--name")
    p = sub.add_parser("normalize")
    p.add_argument("--group", required=True)
    p = sub.add_parser("enrich")
    p.add_argument("stage", nargs="?", default="all")
    p.add_argument("--force", action="store_true",
                   help="reprocess rows whose stage already completed (e.g. after a fix)")
    p.add_argument("--group", required=True)
    p.add_argument("--input", help="path to a file of ids (one per line) to scope this run to")
    p.add_argument("--entity", choices=list(PreparedStore.COLLECTIONS),
                   help="entity the --input ids belong to (required together with --input)")
    p = sub.add_parser("publish")
    p.add_argument("target", choices=["graph"])
    p.add_argument("--group", required=True)
    p = sub.add_parser("dedup")
    p.add_argument("target", choices=["graph", "departments"],
                   help="graph: persons/publications/repositories across every published group; "
                        "departments: fold duplicate Department nodes")
    p.add_argument("--dry-run", action="store_true",
                   help="departments only: compute and journal merges without applying them")
    p.add_argument("--embedder", default="",
                   help="departments only: multilingual embedder for stage-1 blocking "
                        "(labse | minilm | a sentence-transformers model id); default is lexical-only")
    p = sub.add_parser("cache")
    cache_sub = p.add_subparsers(dest="cache_command", required=True)
    p = cache_sub.add_parser("export")
    p.add_argument("--output", type=Path)
    args = parser.parse_args()
    configure_logging(args.verbose)

    if args.command in ("run", "collect", "normalize", "enrich", "publish"):
        mongo = get_mongo_client(settings)
        try:
            db = mongo[settings.mongo_db]
            ensure_indexes(db)
            if args.command == "run":
                group = _group(args)
                _log_result("run", group, PipelineRunner(settings, group, db).run(_selector(args)))
            elif args.command == "collect":
                group = _group(args)
                count = Collector(
                    OpenAlexClient(settings.request_timeout, settings.openalex_api_key),
                    RawStore(db, group)).collect(_selector(args))
                _log_result("collect", group, {"raw_works": count})
            elif args.command == "normalize":
                group = validate_group(args.group)
                result = OpenAlexNormalizer(RawStore(db, group), PreparedStore(db, group)).run()
                _log_result("normalize", group, result)
            elif args.command == "enrich":
                # social_graph is not in ALL_STAGES — it runs by name only,
                # so the check has to know the optional ones too.
                known = {stage.name for stage in (*ALL_STAGES, *OPTIONAL_STAGES)}
                if args.stage != "all" and args.stage not in known:
                    parser.error(f"unknown enrichment stage: {args.stage}")
                if bool(args.input) != bool(args.entity):
                    parser.error("--input requires --entity (and vice versa)")
                group = validate_group(args.group)
                selection = _selection_from_input(args.input, args.entity) if args.input else None
                result = Enricher(PreparedStore(db, group), RawStore(db, group), settings) \
                    .run(args.stage, selection, args.force)
                _log_result(f"enrich {args.stage}", group, result)
            else:
                from pauk.graph.load import load_jsonl_group
                group = validate_group(args.group)
                load_jsonl_group(settings, db, group)
                logger.info("publish graph %s: done", group)
        finally:
            mongo.close()
    elif args.command == "dedup":
        mongo = get_mongo_client(settings)
        try:
            db = mongo[settings.mongo_db]
            ensure_indexes(db)
            if args.target == "departments":
                from pauk.graph.dept_dedup import run_department_dedup
                _log_result("dedup departments", None,
                            run_department_dedup(settings, db, dry_run=args.dry_run,
                                                 embedder=args.embedder))
            else:
                from pauk.graph.dedup import run_graph_dedup
                _log_result("dedup graph", None, run_graph_dedup(settings, db))
        finally:
            mongo.close()
    else:
        from pauk.cache import GraphSnapshotExporter
        path = GraphSnapshotExporter(settings).export(args.output)
        logger.info("cache export: %s", path)


if __name__ == "__main__":
    main()
