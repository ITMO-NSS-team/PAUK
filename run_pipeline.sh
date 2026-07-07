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

run_step "1/8" "init_db" \
    uv run python scripts/init_db.py

run_step "2/8" "populate_publications ($START_DATE -> $END_DATE)" \
    uv run python scripts/populate_publications.py --start-date "$START_DATE" --end-date "$END_DATE"

run_step "3/8" "find_code_links" \
    uv run python scripts/find_code_links.py

run_step "4/8" "enrich_departments" \
    uv run python scripts/enrich_departments.py

run_step "5/8" "enrich_persons_ru" \
    uv run python scripts/enrich_persons_ru.py

run_step "6/8" "build_repositories" \
    uv run python scripts/build_repositories.py

run_step "7/8" "finalize" \
    uv run python scripts/finalize.py

run_step "8/8" "export_graph (данные для визуализации)" \
    uv run python visualization/export_graph.py
