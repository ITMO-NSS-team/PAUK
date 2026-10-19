#!/usr/bin/env bash
# Deploy the site (pauk/gui/web) to the lab server: build it locally (with the
# private data from data/gui/private), rsync it over, restart a screen
# session serving it with scripts/serve_static.py (http.server plus
# precompressed .gz files).
# Requires the ssh key access to REMOTE already set up and the lab VPN on.

set -euo pipefail

REMOTE_HOST="einsteinium.nsslab"
REMOTE="asteb@${REMOTE_HOST}"
REMOTE_DIR="pauk-gui"
SCREEN_NAME="pauk"
PORT="${PORT:-8501}"

root="$(git rev-parse --show-toplevel)"
data_dir="$root/data/gui/private"

# The map can't render without graph-data.json; everything else only leaves
# parts of it empty, so those are a warning the deployer can accept.
if [[ ! -f "$data_dir/graph-data.json" ]]; then
    echo "Missing $data_dir/graph-data.json - run 'pauk gui build' first." >&2
    exit 1
fi

warnings=()
[[ -z "$(git status --porcelain)" ]] || warnings+=("uncommitted changes will end up in the build")
for file in authors-detail.json repos-detail.json pubs-detail.json; do
    [[ -f "$data_dir/$file" ]] || warnings+=("missing $file: cards show no details (run 'pauk gui build')")
done
[[ -f "$data_dir/reports/index.json" ]] \
    || warnings+=("missing reports/index.json: no OSA reports (run 'pauk reports build')")
if (( ${#warnings[@]} > 0 )); then
    printf '  - %s\n' "${warnings[@]}"
    read -r -p "Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || exit 1
fi

echo "==> build pauk/gui/web"
(cd "$root/pauk/gui/web" && npm run build)

# serve_static.py sends these instead of the originals to browsers that
# accept gzip - the JSON shrinks about five times. PDFs and images are
# already compressed, gzip wouldn't gain anything on them.
echo "==> gzip text files in dist"
find "$root/pauk/gui/web/dist" -type f \( -name '*.json' -o -name '*.js' -o -name '*.css' -o -name '*.html' \) \
    -exec gzip -kf -9 {} +

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
rsync -avz --delete "$root/pauk/gui/web/dist/" "$REMOTE:$REMOTE_DIR/"
# Next to the site folder, not inside it - the server script itself isn't served.
rsync -avz "$root/scripts/serve_static.py" "$REMOTE:serve_static.py"

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
screen -dmS $SCREEN_NAME sh -c "python3 \$HOME/serve_static.py $PORT > '\$log' 2>&1"
sleep 2
if ! screen -list | grep -q '\.${SCREEN_NAME}[[:space:]]'; then
    echo "Screen session didn't stay up. Server output (\$log):" >&2
    tail -n 20 "\$log" >&2
    exit 1
fi
EOF

echo "==> done: http://${REMOTE_HOST}:${PORT}"
