from __future__ import annotations

import argparse
import logging

from .barriers import dedup_departments, dedup_persons, finalize_emails, match_github
from .conveyor import Conveyor, merge_by_id
from .io_bridge import (
    INPUT_DIR,
    JsonlSink,
    read_candidate_rows,
    read_departments,
    read_itmo_orgs,
    read_persons,
    read_publications,
)
from .stages import (
    ClassifyRepoLinks,
    CollectEmailsPages,
    CollectEmailsPdf,
    CrossrefOrcid,
    DepartmentRegistry,
    EnrichDepartments,
    EnrichOpenreview,
    EnrichPersons,
    ExtractRepoLinks,
    TranslateNames,
)

logger = logging.getLogger(__name__)


def run(limit: int | None = None, out_dir: str = "data/enriched") -> dict:
    registry = DepartmentRegistry(list(read_departments()))
    persons = {p.id: p for p in read_persons(limit)}
    pubs = list(read_publications())
    if limit:                                     # пробный прогон: только публикации выбранных
        pubs = [pub for pub in pubs if any(a.person_id in persons for a in pub.authors)]
    logger.info("вход: персон %d, публикаций %d, департаментов %d",
                len(persons), len(pubs), len(registry.departments))

    # Субконвейер публикаций, вклады персонам (orcid, email-кандидаты)
    from .conveyor import run_sub
    partials = run_sub(pubs, [CrossrefOrcid(), CollectEmailsPdf()])
    for pid, part in partials.items():
        if pid in persons:                        # партиалы только для персон из потока
            merge_by_id(persons, part)

    # Субконвейер публикаций, ссылки на код по publication_id)
    links = run_sub(pubs, [ExtractRepoLinks(), ClassifyRepoLinks()])

    # Поток персон
    Conveyor(
        TranslateNames(),
        EnrichOpenreview(),
        EnrichPersons(),
        EnrichDepartments(registry),
        CollectEmailsPages(),
    ).run(iter(persons.values()), sink=lambda p: None)

    # Барьеры по всему набору
    bridge = {pub.id: {a.person_id for a in pub.authors} for pub in pubs if pub.authors}
    decisions = match_github(persons, read_candidate_rows(), bridge, read_itmo_orgs())
    d_merged = dedup_departments(registry, persons)
    p_merged = dedup_persons(persons)
    filled = finalize_emails(persons)

    # Сток: JSONL, построчно с flush
    p_sink = JsonlSink(f"{out_dir}/persons.jsonl")
    for p in persons.values():
        p_sink.write_person(p)
    p_sink.close()
    d_sink = JsonlSink(f"{out_dir}/departments.jsonl")
    for d in registry.to_models():
        d_sink.write_model(d)
    d_sink.close()
    l_sink = JsonlSink(f"{out_dir}/repo_links.jsonl")
    for pl in links.values():
        l_sink.write_model(pl)
    l_sink.close()

    stats = {"persons": len(persons), "departments": len(registry.departments),
             "links": len(links), "matched": sum(1 for x in decisions if x["decision"] == "matched"),
             "dedup_departments": d_merged, "dedup_persons": p_merged, "emails_filled": filled}
    logger.info("итог: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Потоковый конвейер обогащения.")
    parser.add_argument("--limit", type=int, default=None, help="Пробный прогон на N персонах.")
    parser.add_argument("--out", default="data/enriched", help="Каталог стока.")
    args = parser.parse_args()
    if not INPUT_DIR.exists():
        raise SystemExit(f"нет входа {INPUT_DIR} — сначала scripts/export_input")
    run(args.limit, args.out)


if __name__ == "__main__":
    main()