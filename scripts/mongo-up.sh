#!/usr/bin/env bash
# (Re)start the pauk-mongo container on the lab server. Idempotent - safe to
# run whether the container doesn't exist yet, exists but is stopped, or is
# already running. Requires ssh key access to REMOTE already set up.
#
# mongo:4.4, not a newer tag - the server's CPU (old Xeon) has no AVX, which
# mongo:5+ requires and crash-loops on ("Illegal instruction").

set -euo pipefail

REMOTE_HOST="einsteinium.nsslab"
REMOTE="asteb@${REMOTE_HOST}"
CONTAINER="pauk-mongo"
IMAGE="mongo:4.4"
DATA_DIR="/home/asteb/pauk-mongo-data"
PORT="27017"

echo "==> ping $REMOTE_HOST"
if ! ping -c 1 -W 2 "$REMOTE_HOST" > /dev/null 2>&1; then
    echo "The server isn't responding to pings - check your VPN." >&2
    exit 1
fi

echo "==> ssh $REMOTE"
if ! ssh -o ConnectTimeout=5 "$REMOTE" true; then
    echo "Failed to connect via SSH." >&2
    exit 1
fi

echo "==> ensure $CONTAINER is up"
ssh "$REMOTE" bash -l <<EOF
set -euo pipefail
if docker ps --format '{{.Names}}' | grep -qx '$CONTAINER'; then
    echo "$CONTAINER already running."
elif docker ps -a --format '{{.Names}}' | grep -qx '$CONTAINER'; then
    echo "$CONTAINER exists but is stopped - starting it."
    docker start '$CONTAINER'
else
    echo "$CONTAINER doesn't exist - creating it."
    mkdir -p '$DATA_DIR'
    docker run -d --name '$CONTAINER' --restart unless-stopped \
        -p ${PORT}:${PORT} -v '$DATA_DIR:/data/db' '$IMAGE'
fi
docker update --restart unless-stopped '$CONTAINER' > /dev/null
EOF

echo "==> done: mongodb://${REMOTE_HOST}:${PORT}"
