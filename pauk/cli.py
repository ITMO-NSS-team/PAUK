from __future__ import annotations

import argparse
from pathlib import Path

from pauk.logging import configure_logging
from pauk.models import Person, Publication
from pauk.pipeline.collect import Collector
from pauk.pipeline.enrich import Enricher
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages import ALL_STAGES
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.runner import PipelineRunner
from pauk.pipeline.selectors import PeriodSelector, WorkSelector, WorksFileSelector
from pauk.settings import settings
from pauk.sources import OpenAlexClient
from pauk.storage import PreparedStore, RawStore
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


def _input_group_and_selection(value: str) -> tuple[str, PreparedSelection | None]:
    path = Path(value).resolve()
    prepared_root = settings.prepared_dir.resolve()
    if path.is_dir():
        if path.parent != prepared_root:
            raise ValueError(f"prepared group directory must be directly inside {prepared_root}")
        return validate_group(path.name), None
    if not path.is_file():
        raise ValueError(f"prepared input file does not exist: {path}")
    if path.parent.parent != prepared_root:
        raise ValueError(f"prepared entity file must be directly inside a group in {prepared_root}")
    group = validate_group(path.parent.name)
    entity = PreparedStore.entity_for_path(path)
    model_map = {
        "publications": Publication, "persons": Person, "departments": None,
        "repositories": None, "github_profiles": None, "repo_links": None,
    }
    if model_map[entity] is None:
        rows = PreparedStore(settings.prepared_dir, group).read_rows(entity)
        ids = frozenset(str(row.get("id") or row.get("publication_id")) for row in rows)
    else:
        ids = frozenset(row.id for row in PreparedStore(settings.prepared_dir, group).read_models(entity, model_map[entity]))
    return group, PreparedSelection(entity, ids)


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
    input_group = p.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--group")
    input_group.add_argument("--input")
    p = sub.add_parser("publish")
    p.add_argument("target", choices=["graph"])
    input_group = p.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--group")
    input_group.add_argument("--input")
    p = sub.add_parser("dedup")
    p.add_argument("target", choices=["graph"],
                   help="deduplicate persons across every published group in the graph")
    p = sub.add_parser("cache")
    cache_sub = p.add_subparsers(dest="cache_command", required=True)
    p = cache_sub.add_parser("export")
    p.add_argument("--output", type=Path)
    args = parser.parse_args()
    configure_logging(args.verbose)
    if args.command == "run":
        print(PipelineRunner(settings, _group(args)).run(_selector(args)))
    elif args.command == "collect":
        group = _group(args)
        print({"raw_works": Collector(OpenAlexClient(settings.request_timeout, settings.openalex_api_key), RawStore(settings.raw_dir, group)).collect(_selector(args))})
    elif args.command == "normalize":
        group = validate_group(args.group)
        print(OpenAlexNormalizer(RawStore(settings.raw_dir, group), PreparedStore(settings.prepared_dir, group)).run())
    elif args.command == "enrich":
        if args.stage != "all" and args.stage not in {stage.name for stage in ALL_STAGES}:
            parser.error(f"unknown enrichment stage: {args.stage}")
        group, selection = (validate_group(args.group), None) if args.group else _input_group_and_selection(args.input)
        print(Enricher(PreparedStore(settings.prepared_dir, group), RawStore(settings.raw_dir, group), settings).run(args.stage, selection, args.force))
    elif args.command == "publish":
        from pauk.graph.load import load_jsonl_group
        group, _selection = (validate_group(args.group), None) if args.group else _input_group_and_selection(args.input)
        load_jsonl_group(settings, group)
    elif args.command == "dedup":
        from pauk.graph.dedup import run_graph_dedup
        print(run_graph_dedup(settings))
    else:
        from pauk.cache import GraphSnapshotExporter
        print(GraphSnapshotExporter(settings).export(args.output))


if __name__ == "__main__":
    main()
