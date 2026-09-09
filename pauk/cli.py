from __future__ import annotations

import argparse
import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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

logger = logging.getLogger("pauk.cli")


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
    return validate_group(
        group_name(
            work_id=selector.work_id if isinstance(selector, WorkSelector) else None,
            date_from=selector.date_from if isinstance(selector, PeriodSelector) else None,
            date_to=selector.date_to if isinstance(selector, PeriodSelector) else None,
            name=args.name,
        )
    )


def _selection_from_input(path: str, entity: str) -> PreparedSelection:
    ids = frozenset(
        line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
    )
    return PreparedSelection(entity, ids)


def _log_result(action: str, group: str | None, result: dict) -> None:
    summary = ", ".join(f"{key}={value}" for key, value in result.items()) or "nothing to do"
    logger.info("%s: %s", f"{action} {group}" if group else action, summary)


@contextmanager
def _mongo_db() -> Generator[Any, None, None]:
    """Opens a Mongo connection for the duration of one command, runs
    ensure_indexes() once, closes on exit - shared by every command that
    touches Mongo (run/collect/normalize/enrich/publish/dedup) instead of
    each repeating its own try/finally.
    """
    mongo = get_mongo_client(settings)
    try:
        db = mongo[settings.mongo_db]
        ensure_indexes(db)
        yield db
    finally:
        mongo.close()


def _add_pipeline_parsers(sub: argparse._SubParsersAction) -> None:
    """Registers the OpenAlex/Mongo pipeline subcommands: run, collect,
    normalize, enrich, publish, dedup."""
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
    p.add_argument(
        "--force",
        action="store_true",
        help="reprocess rows whose stage already completed (e.g. after a fix)",
    )
    p.add_argument("--group", required=True)
    p.add_argument("--input", help="path to a file of ids (one per line) to scope this run to")
    p.add_argument(
        "--entity",
        choices=list(PreparedStore.COLLECTIONS),
        help="entity the --input ids belong to (required together with --input)",
    )
    p = sub.add_parser("publish")
    p.add_argument("target", choices=["graph"])
    p.add_argument("--group", required=True)
    p = sub.add_parser("dedup")
    p.add_argument(
        "target",
        choices=["graph"],
        help="deduplicate persons across every published group in the graph",
    )


def _add_cache_parsers(sub: argparse._SubParsersAction) -> None:
    """Registers `cache export`/`cache inspect` - the Neo4j snapshot subcommands."""
    p = sub.add_parser("cache")
    cache_sub = p.add_subparsers(dest="cache_command", required=True)
    p = cache_sub.add_parser("export", help="snapshot the graph from Neo4j to a file")
    p.add_argument("--output", type=Path, help="default: cache_dir/graph_snapshot_<date>.json")
    p = cache_sub.add_parser(
        "inspect",
        help="print table sizes / field stats / sample rows from a snapshot",
        usage="pauk cache inspect [-h] [path_to_snapshot] [--table TABLE] [--sample SAMPLE]",
    )
    p.add_argument(
        "snapshot",
        type=Path,
        nargs="?",
        default=None,
        metavar="path_to_snapshot",
        help="snapshot file (default: newest in cache_dir)",
    )

    p.add_argument(
        "--table",
        help="name of the table to inspect (run 'pauk cache inspect' with no flags to see the table names)",
    )
    p.add_argument(
        "--sample",
        type=int,
        default=0,
        help="print N sample rows from --table instead of its field stats",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pauk")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_pipeline_parsers(sub)
    _add_cache_parsers(sub)
    return parser


def _cmd_run(args) -> None:
    with _mongo_db() as db:
        group = _group(args)
        _log_result("run", group, PipelineRunner(settings, group, db).run(_selector(args)))


def _cmd_collect(args) -> None:
    with _mongo_db() as db:
        group = _group(args)
        count = Collector(
            OpenAlexClient(settings.request_timeout, settings.openalex_api_key), RawStore(db, group)
        ).collect(_selector(args))
        _log_result("collect", group, {"raw_works": count})


def _cmd_normalize(args) -> None:
    with _mongo_db() as db:
        group = validate_group(args.group)
        result = OpenAlexNormalizer(RawStore(db, group), PreparedStore(db, group)).run()
        _log_result("normalize", group, result)


def _cmd_enrich(args, parser: argparse.ArgumentParser) -> None:
    # social_graph is not in ALL_STAGES — it runs by name only, so the check
    # has to know the optional ones too.
    known = {stage.name for stage in (*ALL_STAGES, *OPTIONAL_STAGES)}
    if args.stage != "all" and args.stage not in known:
        parser.error(f"unknown enrichment stage: {args.stage}")
    if bool(args.input) != bool(args.entity):
        parser.error("--input requires --entity (and vice versa)")
    with _mongo_db() as db:
        group = validate_group(args.group)
        selection = _selection_from_input(args.input, args.entity) if args.input else None
        result = Enricher(PreparedStore(db, group), RawStore(db, group), settings).run(
            args.stage, selection, args.force
        )
        _log_result(f"enrich {args.stage}", group, result)


def _cmd_publish(args) -> None:
    from pauk.graph.load import load_jsonl_group

    with _mongo_db() as db:
        group = validate_group(args.group)
        load_jsonl_group(settings, db, group)
        logger.info("publish graph %s: done", group)


def _cmd_dedup() -> None:
    # args.target only ever validates as "graph" (argparse choices=["graph"])
    # - nothing branches on it, there is no other target to dispatch on.
    from pauk.graph.dedup import run_graph_dedup

    with _mongo_db() as db:
        _log_result("dedup graph", None, run_graph_dedup(settings, db))


def _cmd_cache_export(args) -> None:
    from pauk.cache import GraphSnapshotExporter

    path = GraphSnapshotExporter(settings).export(args.output)
    logger.info("cache export: %s", path)


def _cmd_cache_inspect(args, parser: argparse.ArgumentParser) -> None:
    from pauk.cache.graph_snapshot import latest_snapshot, read_snapshot
    from pauk.cache.inspect import describe_table, sample_rows, summarize

    path = args.snapshot or latest_snapshot(settings.cache_dir)
    logger.info("cache inspect: %s", path)
    graph = read_snapshot(path)
    if args.table is None:
        summarize(graph)
    elif args.table not in graph:
        parser.error(f"unknown table: {args.table!r}. Available: {', '.join(graph)}")
    elif args.sample:
        sample_rows(graph[args.table], args.sample)
    else:
        describe_table(graph[args.table], args.table)


def _cmd_cache(args, parser: argparse.ArgumentParser) -> None:
    if args.cache_command == "export":
        _cmd_cache_export(args)
    elif args.cache_command == "inspect":
        _cmd_cache_inspect(args, parser)
    else:
        parser.error(f"unknown cache command: {args.cache_command}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose)

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "collect":
        _cmd_collect(args)
    elif args.command == "normalize":
        _cmd_normalize(args)
    elif args.command == "enrich":
        _cmd_enrich(args, parser)
    elif args.command == "publish":
        _cmd_publish(args)
    elif args.command == "dedup":
        _cmd_dedup()
    elif args.command == "cache":
        _cmd_cache(args, parser)
    else:
        parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
