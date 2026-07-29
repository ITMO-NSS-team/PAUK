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

run_step "1/4" "init_db схема" \
    uv run python -m scripts.init_db

run_step "2/4" "populate_publications - ingest из OpenAlex ($START_DATE -> $END_DATE)" \
    uv run python -m scripts.populate_publications --start-date "$START_DATE" --end-date "$END_DATE"

run_step "3/4" "export_input - SQLite -> JSONL (мост, умрёт с JSON-вводом)" \
    uv run python -m scripts.export_input

run_step "4/4" "конвейер обогащения" \
    uv run python -m data_enrichment.run_conveyor

run_step "5/5" "export_graph данные для визуализации" \
    uv run python visualization/export_graph.py