#!/usr/bin/env bash
#
# Запускает все шаги пайплайна по порядку.
#
# Использование:
#   ./run_pipeline.sh                                # обычный запуск
#   ./run_pipeline.sh 2>&1 | tee logs/pipeline.log   # с логом всего вывода
#
# Параметры для populate_publications.py меняются здесь, остальные шаги
# параметров не требуют.

set -euo pipefail

START_DATE="2025-01-01"
END_DATE="2026-06-03"
FETCH_LIMIT="10000"
CLASSIFY_LIMIT="10000"

cd "$(dirname "$0")"

run_step() {
    local n="$1"
    local title="$2"
    shift 2
    echo
    echo "================================================================"
    echo "[$n] $title"
    echo "================================================================"
    "$@"
}

run_step "1/6" "init_db" \
    uv run python scripts/init_db.py

run_step "2/6" "populate_publications ($START_DATE -> $END_DATE)" \
    uv run python scripts/populate_publications.py \
        --start-date "$START_DATE" --end-date "$END_DATE"

run_step "3/7" "fetch_papers (--limit $FETCH_LIMIT)" \
    uv run python scripts/fetch_papers.py --limit "$FETCH_LIMIT"

# Дозагрузка недостающих PDF из S3-зеркала препринтов. Без S3-кредов в
# .env шаг завершается сразу без изменений.
run_step "4/7" "fetch_pdfs_s3 (дозагрузка недостающих PDF)" \
    uv run python scripts/fetch_pdfs_s3.py

run_step "5/7" "extract_repo_links" \
    uv run python scripts/extract_repo_links.py

run_step "6/7" "classify_repo_links (--limit $CLASSIFY_LIMIT)" \
    uv run python scripts/classify_repo_links.py --limit "$CLASSIFY_LIMIT"

run_step "7/7" "sync_publications" \
    uv run python scripts/sync_publications.py