#!/usr/bin/env bash
# Gaffer refresh — runs the pipeline and publishes fresh JSON.
# Intended to run on the Mac Mini via launchd (nightly + pre-deadline).
#
#   1. run the pipeline (ingest -> project -> optimise -> export data/*.json)
#   2. copy artifacts into the front-end's public/data so the site ships them
#   3. commit + push; a GitHub Action rebuilds and deploys the Pages site
#
# Usage: scripts/refresh.sh  [--fast]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

PY="$REPO/.venv/bin/python"            # macOS venv layout
[ -x "$PY" ] || PY="$REPO/.venv/Scripts/python.exe"   # windows fallback

echo "[gaffer] pipeline…"
"$PY" -m gaffer.pipeline "$@"

echo "[gaffer] staging artifacts for the front-end…"
mkdir -p web/public/data
cp data/*.json web/public/data/

if [ -n "$(git status --porcelain data web/public/data)" ]; then
  git add data web/public/data
  git commit -m "data: refresh $(date -u +%Y-%m-%dT%H:%MZ)"
  git push
  echo "[gaffer] pushed fresh data."
else
  echo "[gaffer] no data changes."
fi
