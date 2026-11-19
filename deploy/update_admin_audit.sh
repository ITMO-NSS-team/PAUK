#!/usr/bin/env bash
# Run on the server after checking out the reviewed fix.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
env_file="${1:-/home/asteb/pauk/secrets/pauk.env}"
compose=(sudo docker compose --env-file "$env_file" -f deploy/compose.yml)

"${compose[@]}" build admin

# Check the bind mount as the application user before replacing the service.
"${compose[@]}" run --rm --no-deps -T --entrypoint python admin -c '
from tempfile import TemporaryFile

from pauk.settings import Settings

directory = Settings().audit_dir
directory.mkdir(parents=True, exist_ok=True)
with TemporaryFile(dir=directory) as stream:
    stream.write(b"audit mount check\n")
    stream.flush()
with (directory / "audit.jsonl").open("a", encoding="utf-8"):
    pass
print("audit directory and journal are writable")
'

"${compose[@]}" up -d --no-deps --force-recreate admin

"${compose[@]}" exec -T admin python -c '
from pauk.graph.audit import SharedGraph
from pauk.graph.mutations import count_nodes
from pauk.settings import Settings

graph = SharedGraph(Settings(), connection_timeout=5, retry_time=0)
try:
    print(count_nodes(graph.audited(actor="deployment-check", source="admin-ui")))
finally:
    graph.close()
'
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 2 \
    http://127.0.0.1:8505/login --output /dev/null
"${compose[@]}" ps admin
