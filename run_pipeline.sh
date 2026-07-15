#!/usr/bin/env bash
# ./run_pipeline.sh 2>&1 | tee logs/pipeline.log

set -euo pipefail

START_DATE="2020-01-01"
END_DATE="2026-07-04"

cd "$(dirname "$0")"

run_step() {
    local n="$1" title="$2"
    shift 2
    echo
    echo "================================================================"
    echo "[$n] $title"
    echo "================================================================"
    "$@"
}

run_step "1/10" "init_db" \
    uv run python scripts/init_db.py

run_step "2/10" "populate_publications ($START_DATE -> $END_DATE)" \
    uv run python scripts/populate_publications.py --start-date "$START_DATE" --end-date "$END_DATE"

run_step "3/10" "find_code_links" \
    uv run python scripts/find_code_links.py

run_step "4/10" "seed_departments (официальный en<->ru каталог -> departments)" \
    uv run python scripts/seed_departments.py

run_step "5/10" "enrich_departments --mode match (stage-1 каталог + stage-2 LLM)" \
    uv run python scripts/enrich_departments.py --mode match

run_step "6/10" "enrich_departments --mode translate (name_ru неофициальных)" \
    uv run python scripts/enrich_departments.py --mode translate

run_step "7/10" "enrich_persons_ru" \
    uv run python scripts/enrich_persons_ru.py

run_step "8/10" "build_repositories" \
    uv run python scripts/build_repositories.py

run_step "9/10" "finalize" \
    uv run python scripts/finalize.py

run_step "10/10" "export_graph (данные для визуализации)" \
    uv run python visualization/export_graph.py
