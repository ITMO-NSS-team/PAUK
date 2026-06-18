#!/usr/bin/env bash
# ./run_pipeline.sh 2>&1 | tee logs/pipeline.log

set -euo pipefail

START_DATE="2024-05-01"
END_DATE="2026-06-15"

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

run_step "1/7" "init_db" \
    uv run python scripts/init_db.py

run_step "2/7" "populate_publications ($START_DATE -> $END_DATE)" \
    uv run python scripts/populate_publications.py --start-date "$START_DATE" --end-date "$END_DATE"

run_step "3/7" "find_code_links" \
    uv run python scripts/find_code_links.py

run_step "4/7" "enrich_departments" \
    uv run python scripts/enrich_departments.py

run_step "5/7" "enrich_persons_ru" \
    uv run python scripts/enrich_persons_ru.py

run_step "6/7" "build_repositories" \
    uv run python scripts/build_repositories.py

run_step "7/7" "finalize" \
    uv run python scripts/finalize.py
