#!/usr/bin/env bash
set -euo pipefail

repo="ITMO-NSS-team/PAUK"
tag="pages-data"
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
public_dir="${PAUK_PUBLIC_DIR:-$root/data/gui/public}"
reports_dir="${PAUK_REPORTS_DIR:-$root/data/gui/private/reports}"
export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:?GITHUB_TOKEN is required}}"

python3 - "$public_dir" <<'PY'
import json
import re
import sys
from pathlib import Path
folder = Path(sys.argv[1])
for name in ('graph-data.json', 'authors-detail.json', 'repos-detail.json', 'pubs-detail.json'):
    path = folder / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f'Missing public file: {name}')
graph = json.loads((folder / 'graph-data.json').read_text())
authors = json.loads((folder / 'authors-detail.json').read_text())
if any(not re.fullmatch(r'a\d+', row.get('key', '')) for row in graph['authors']):
    raise SystemExit('Public author identifiers are not anonymized')
if any(set(row) - {'key', 'pub_roles'} for row in authors):
    raise SystemExit('Personal author fields in public data')
PY

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/site"
for file in graph-data.json authors-detail.json repos-detail.json pubs-detail.json; do
    cp "$public_dir/$file" "$work/site/$file"
done
if [[ -f "$reports_dir/index.json" ]]; then
    cp -RL "$reports_dir" "$work/site/reports"
fi
COPYFILE_DISABLE=1 tar -czf "$work/pages-data.tar.gz" -C "$work/site" .

gh release view "$tag" --repo "$repo" >/dev/null 2>&1 \
    || gh release create "$tag" --repo "$repo" --prerelease --title "Public map data" \
        --notes "Public map data for GitHub Pages."
gh release upload "$tag" "$work/pages-data.tar.gz" --repo "$repo" --clobber
actor="$(gh api user --jq .login)"
previous="$(gh run list --repo "$repo" --workflow pages.yml --branch main --event workflow_dispatch --user "$actor" --limit 1 --json databaseId --jq '.[0].databaseId // 0')"
gh workflow run pages.yml --repo "$repo" --ref main
run=""
for attempt in {1..30}; do
    candidate="$(gh run list --repo "$repo" --workflow pages.yml --branch main --event workflow_dispatch --user "$actor" --limit 1 --json databaseId --jq '.[0].databaseId // 0')"
    if [[ "$candidate" =~ ^[0-9]+$ && "$candidate" -gt "$previous" ]]; then
        run="$candidate"
        break
    fi
    sleep 2
done
if [[ -z "$run" ]]; then
    echo "Pages workflow did not appear after dispatch" >&2
    exit 1
fi
gh run watch "$run" --repo "$repo" --exit-status
if [[ -n "${PAUK_PAGES_RESULT:-}" ]]; then
    gh run view "$run" --repo "$repo" --json headSha,headBranch,url,conclusion > "$PAUK_PAGES_RESULT"
fi
