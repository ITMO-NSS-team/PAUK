import argparse
import logging

from .config import DB_PATH
from .connector import SqliteConnector
from .operations import (
    BuildRepositories,
    ClassifyRepoLinks,
    CollectEmailsPages,
    CollectEmailsPdf,
    CrossrefOrcid,
    DedupFinalize,
    EnrichDepartments,
    EnrichOpenreview,
    EnrichPersons,
    ExpandAccounts,
    ExtractRepoLinks,
    HarvestRepos,
    MatchGithub,
    MergeProfiles,
    TranslateNames,
)
from .orchestrator import Context, Loop, Op, Orchestrator, Parallel, Sequence
from .ratelimit import RateLimiter


def build_pipeline() -> Sequence:
    return Sequence(
        "Пайплайн обработки",

        # Слой 1 параллельно
        Parallel(
            "Слой 1 · вход-зависимые",
            Op(CrossrefOrcid()),
            Sequence("ссылки на код", Op(ExtractRepoLinks()), Op(ClassifyRepoLinks())),
            Op(CollectEmailsPdf()),
            Op(EnrichDepartments()),
            Op(TranslateNames()),
        ),

        # Слой 2
        Op(EnrichPersons()),

        # Слой 3 репозитории из подтверждённых ссылок (GitHub API)
        Op(BuildRepositories()),

        # Слой 4 две ветки параллельно
        Parallel(
            "Слой 4 две независимые ветки",
            Sequence(
                "ветка email",
                Op(EnrichOpenreview()),
                Op(CollectEmailsPages()),
            ),
            Sequence(
                "ветка соцграф",
                Op(HarvestRepos()),
                Loop(
                    "расширение соцграфа",
                    Sequence("harvest и match", Op(ExpandAccounts()), Op(MatchGithub())),
                    max_iterations=5,
                ),
            ),
        ),

        # Слой 5 сборка всех источников
        Op(MergeProfiles()),

        # Слой 6 дедуп по всему набору
        Op(DedupFinalize()),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Оркестратор обработки данных PAUK.")
    p.add_argument("--db", default=str(DB_PATH),
                   help="Путь к SQLite (пока; коннектор скроет это при переезде на Neo4j).")
    p.add_argument("--unit-workers", type=int, default=8,
                   help="Параллельных батчей внутри операции.")
    p.add_argument("--rps", type=float, default=5.0, help="Общий лимит запросов/сек к внешним API.")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    connector = SqliteConnector(args.db)
    ctx = Context(
        connector=connector,
        rate_limiter=RateLimiter(args.rps),
        unit_workers=args.unit_workers,
    )
    try:
        connector.ensure_schema()  # служебные таблицы-выходы
        Orchestrator(ctx).run(build_pipeline())
        connector.rebuild_derived()  # sync_*
    finally:
        connector.close()


if __name__ == "__main__":
    main()