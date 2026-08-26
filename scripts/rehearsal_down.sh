#!/usr/bin/env bash
# Remove the rehearsal containers and the restored copy of the graph.
# Run from a normal terminal, like scripts/rehearsal_up.sh.
set -euo pipefail

docker rm -f pauk-rehearsal-neo4j pauk-rehearsal-mongo >/dev/null 2>&1 || true
docker volume rm -f pauk-rehearsal-neo4j >/dev/null 2>&1 || true
echo "rehearsal containers and volume removed"
