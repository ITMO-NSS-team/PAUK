#!/usr/bin/env bash
# Publish the public map (masked names, no personal fields) to GitHub Pages:
# pack data/gui/public plus the OSA reports, upload them to the `pages-data`
# release and run .github/workflows/pages.yml, which builds the site from main
# with `npm run build:public` and deploys it.
# Requires `pauk gui build` to have run, and `gh` logged in with write access.

set -euo pipefail

REPO="ITMO-NSS-team/PAUK"
TAG="pages-data"

root="$(git rev-parse --show-toplevel)"
public_dir="$root/data/gui/public"
reports_dir="$root/data/gui/private/reports"

for file in graph-data.json authors-detail.json repos-detail.json pubs-detail.json; do
    if [[ ! -f "$public_dir/$file" ]]; then
        echo "Missing $public_dir/$file - run 'pauk gui build' first." >&2
        exit 1
    fi
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/site"
cp "$public_dir"/*.json "$work/site/"
# The reports describe public repositories only; they live in private/ because
# `pauk reports build` writes there for the lab server.
if [[ -f "$reports_dir/index.json" ]]; then
    cp -RL "$reports_dir" "$work/site/reports"
else
    echo "warning: no $reports_dir/index.json - the site goes out without OSA reports" >&2
fi

echo "==> pack the public data"
tar -czf "$work/pages-data.tar.gz" -C "$work/site" .
du -h "$work/pages-data.tar.gz"

echo "==> upload to the $TAG release"
gh release view "$TAG" --repo "$REPO" > /dev/null 2>&1 \
    || gh release create "$TAG" --repo "$REPO" --prerelease --title "Public map data" \
        --notes "Data for the GitHub Pages map, uploaded by scripts/publish_pages.sh. Not a software release."
gh release upload "$TAG" "$work/pages-data.tar.gz" --repo "$REPO" --clobber

echo "==> run the Pages workflow"
gh workflow run pages.yml --repo "$REPO" --ref main
echo "Follow it with: gh run watch --repo $REPO \$(gh run list --repo $REPO --workflow pages.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
