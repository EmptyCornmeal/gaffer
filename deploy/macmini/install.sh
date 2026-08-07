#!/bin/sh
# Render the Gaffer notification launchd job from its template.
#
# This script validates and writes. It does NOT run launchctl — loading the job
# is always your decision, made after reading the rendered file.
set -eu

ROOT="${1:-}"
if [ -z "$ROOT" ]; then
  echo "usage: ./install.sh /absolute/path/to/gaffer [python]" >&2
  exit 2
fi

case "$ROOT" in
  /*) ;;
  *) echo "error: the repo path must be absolute, got '$ROOT'" >&2; exit 2 ;;
esac

PYTHON="${2:-$ROOT/.venv/bin/python}"
TEMPLATE="$(cd "$(dirname "$0")" && pwd)/com.gaffer.notify.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.gaffer.notify.plist"

# --- validate every path BEFORE writing anything ---------------------------
fail=0
[ -d "$ROOT" ]                || { echo "error: no such directory: $ROOT" >&2; fail=1; }
[ -d "$ROOT/src/gaffer" ]     || { echo "error: $ROOT is not a Gaffer checkout (no src/gaffer)" >&2; fail=1; }
[ -x "$PYTHON" ]              || { echo "error: python not executable: $PYTHON" >&2; fail=1; }
[ -f "$TEMPLATE" ]            || { echo "error: template missing: $TEMPLATE" >&2; fail=1; }
[ "$fail" -eq 0 ]             || { echo "refusing to install with unresolved paths" >&2; exit 1; }

# Prove the entry point actually runs before scheduling it.
if ! (cd "$ROOT" && "$PYTHON" -m gaffer.notify --help >/dev/null 2>&1); then
  echo "error: '$PYTHON -m gaffer.notify' failed. Install the package first:" >&2
  echo "         cd $ROOT && $PYTHON -m pip install -e '.[dev]'" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
if [ -e "$TARGET" ]; then
  echo "note: $TARGET already exists; writing $TARGET.new instead"
  TARGET="$TARGET.new"
fi

sed -e "s|__GAFFER_ROOT__|$ROOT|g" -e "s|__PYTHON__|$PYTHON|g" "$TEMPLATE" > "$TARGET"

echo "wrote $TARGET"
echo
echo "It is a DRY RUN: the job has no --send flag, so it delivers nothing."
echo
echo "Next steps, in order:"
echo "  1. cat '$TARGET'                       # read it"
echo "  2. cd '$ROOT' && '$PYTHON' -m gaffer.notify   # dry run by hand"
echo "  3. launchctl load '$TARGET'            # only when YOU are ready"
