#!/usr/bin/env bash
# Deploy new_gui to the lab server: build the static site locally (with the
# private data from data/gui/private), rsync it over, restart a screen
# session serving it with python3 http.server. Runs next to the old GUI
# (pauk.gui.serve on 8501, see scripts/deploy.sh) and does not touch it.
# Requires the ssh key access to REMOTE already set up and the lab VPN on.

set -euo pipefail

REMOTE_HOST="einsteinium.nsslab"
REMOTE="asteb@${REMOTE_HOST}"
REMOTE_DIR="pauk-new-gui"
SCREEN_NAME="pauk-new-gui"
PORT="${PORT:-8502}"

root="$(git rev-parse --show-toplevel)"
data_dir="$root/data/gui/private"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "There are uncommitted changes - they will end up in the build."
    read -r -p "Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || exit 1
fi

for file in graph-data.json authors-detail.json repos-detail.json pubs-detail.json; do
    if [[ ! -f "$data_dir/$file" ]]; then
        echo "Missing $data_dir/$file - run new_generate/graph_builder.py first." >&2
        exit 1
    fi
done

echo "==> build new_gui"
(cd "$root/new_gui" && npm run build)

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

echo "==> rsync dist to $REMOTE:$REMOTE_DIR"
rsync -avz --delete "$root/new_gui/dist/" "$REMOTE:$REMOTE_DIR/"

echo "==> restart screen '$SCREEN_NAME' on port $PORT"
ssh "$REMOTE" bash -l <<EOF
set -e
if ! command -v python3 > /dev/null; then
    echo "python3 not found on the server." >&2
    exit 1
fi
python3 --version
if screen -list | grep -q '\.${SCREEN_NAME}[[:space:]]'; then
    screen -S $SCREEN_NAME -X quit
fi
# cd instead of --directory: that flag needs Python 3.7+. The server's own
# output goes to a log outside the served folder, so a crash is diagnosable.
log="\$HOME/${SCREEN_NAME}.log"
cd "\$HOME/$REMOTE_DIR"
screen -dmS $SCREEN_NAME sh -c "python3 -m http.server $PORT > '\$log' 2>&1"
sleep 2
if ! screen -list | grep -q '\.${SCREEN_NAME}[[:space:]]'; then
    echo "Screen session didn't stay up. Server output (\$log):" >&2
    tail -n 20 "\$log" >&2
    exit 1
fi
EOF

echo "==> done: http://${REMOTE_HOST}:${PORT}"
