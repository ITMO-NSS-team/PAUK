#!/usr/bin/env bash
# Bring up a throwaway copy of production — Neo4j restored from the latest
# dump, Mongo loaded from the JSONL snapshots — so a pipeline run can be
# rehearsed end to end without touching the server.
#
# Run this from a normal terminal (it needs docker group membership, which a
# process started before `usermod -aG docker` does not have).
#
#   bash scripts/rehearsal_up.sh
#
# Tear down again with scripts/rehearsal_down.sh.
set -euo pipefail

cd "$(dirname "$0")/.."

# Same version and edition as the server: a dump restored into a newer Neo4j
# would silently upgrade the store, and then the rehearsal is not a rehearsal.
IMAGE=neo4j:2026.05.0-community
MONGO_IMAGE=mongo:7
DUMP_DIR="$PWD/data/backups/neo4j-dump"
MONGO_SNAPSHOT="$PWD/data/backups/mongo-before-works-2026-08-24"
VOLUME=pauk-rehearsal-neo4j
PASSWORD=rehearsal

# Collections the pipeline reads or merges into. Loading them is what makes
# the rehearsal faithful: an author who already exists must be *merged* with,
# not created fresh, and that path only exists when the row is there.
COLLECTIONS=(publications persons departments organizations
             repositories repo_links github_profiles)

[ -f "$DUMP_DIR/neo4j.dump" ] || { echo "missing $DUMP_DIR/neo4j.dump" >&2; exit 1; }
[ -d "$MONGO_SNAPSHOT" ] || { echo "missing $MONGO_SNAPSHOT" >&2; exit 1; }

echo "--- removing anything left from a previous run ---"
docker rm -f pauk-rehearsal-neo4j pauk-rehearsal-mongo >/dev/null 2>&1 || true
docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true

echo "--- restoring the graph dump into a fresh volume ---"
docker run --rm \
    -v "$VOLUME":/data \
    -v "$DUMP_DIR":/dumps:ro \
    "$IMAGE" \
    neo4j-admin database load neo4j --from-path=/dumps --overwrite-destination=true

echo "--- starting neo4j on 7688 ---"
# Ports deliberately not the defaults: a production Neo4j may be reachable on
# 7687 from this machine, and the rehearsal must not be able to hit it.
docker run -d --name pauk-rehearsal-neo4j \
    -p 7688:7687 -p 7475:7474 \
    -v "$VOLUME":/data \
    -e NEO4J_AUTH="neo4j/$PASSWORD" \
    "$IMAGE" >/dev/null

echo "--- starting mongo on 27018 ---"
docker run -d --name pauk-rehearsal-mongo \
    -p 27018:27017 \
    -v "$MONGO_SNAPSHOT":/snapshot:ro \
    "$MONGO_IMAGE" >/dev/null

echo -n "--- waiting for mongo "
for _ in $(seq 1 60); do
    if docker exec pauk-rehearsal-mongo mongosh --quiet --eval 'db.adminCommand("ping")' \
        >/dev/null 2>&1; then
        echo "ready"
        break
    fi
    echo -n "."
    sleep 2
done

echo "--- loading the collection snapshots ---"
for name in "${COLLECTIONS[@]}"; do
    # The snapshots are Extended JSON written by bson.json_util, which is what
    # mongoimport reads natively — dates and ObjectIds survive the round trip.
    docker exec pauk-rehearsal-mongo mongoimport \
        --quiet --db pauk --collection "$name" --drop \
        --file "/snapshot/${name}.jsonl" 2>&1 | tail -1
    count=$(docker exec pauk-rehearsal-mongo mongosh --quiet pauk \
        --eval "db.${name}.countDocuments({})")
    printf '  %-16s %s\n' "$name" "$count"
done

echo -n "--- waiting for neo4j "
for _ in $(seq 1 90); do
    if docker exec pauk-rehearsal-neo4j cypher-shell -u neo4j -p "$PASSWORD" \
        "RETURN 1;" >/dev/null 2>&1; then
        echo "ready"
        break
    fi
    echo -n "."
    sleep 2
done

echo
echo "graph in the rehearsal copy:"
docker exec pauk-rehearsal-neo4j cypher-shell -u neo4j -p "$PASSWORD" --format plain \
    "MATCH (n) RETURN labels(n) AS labels, count(*) AS c ORDER BY c DESC;"

echo
echo "ready. bolt://localhost:7688  (neo4j/$PASSWORD)   mongodb://localhost:27018"
