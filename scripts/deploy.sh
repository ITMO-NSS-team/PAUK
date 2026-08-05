#!/usr/bin/env bash
# Redeploy pauk.gui.serve on the lab server: pull latest main, restart the
# screen session. Requires the ssh key access to REMOTE already set up.

set -euo pipefail

REMOTE_HOST="einsteinium.nsslab"
REMOTE="asteb@${REMOTE_HOST}"
REMOTE_DIR="PAUK"
SCREEN_NAME="pauk"

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" != "main" ]]; then
    echo "Current branch - '$branch', not 'main'. The 'main' branch will get pulled on the server anyway."
    read -r -p "Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || exit 1
fi

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

echo "==> git pull on server"
ssh "$REMOTE" bash -l <<EOF
cd $REMOTE_DIR && git pull origin main
EOF

echo "==> restart screen '$SCREEN_NAME'"
ssh "$REMOTE" bash -l <<EOF
if screen -list | grep -q '\.${SCREEN_NAME}[[:space:]]'; then
    screen -S $SCREEN_NAME -X quit
fi
cd $REMOTE_DIR && screen -dmS $SCREEN_NAME uv run python -m pauk.gui.serve
sleep 1
if ! screen -list | grep -q '\.${SCREEN_NAME}[[:space:]]'; then
    echo "Screen session didn't stay up - check 'uv' is on PATH on the server." >&2
    exit 1
fi
EOF

echo "==> done: http://${REMOTE_HOST}:8501"
