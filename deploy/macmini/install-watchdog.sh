#!/bin/sh
# Render the Gaffer refresh watchdog launchd job from its template.
#
# Same contract as install.sh: this script validates and writes. It does NOT
# run launchctl. Loading the job is your decision, made after reading the
# rendered file.
set -eu

ROOT="${1:-}"
if [ -z "$ROOT" ]; then
  echo "usage: ./install-watchdog.sh /absolute/path/to/gaffer [python]" >&2
  exit 2
fi

case "$ROOT" in
  /*) ;;
  *) echo "error: the repo path must be absolute, got '$ROOT'" >&2; exit 2 ;;
esac

PYTHON="${2:-$ROOT/.venv/bin/python}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$HERE/com.myles.gaffer-watchdog.plist.template"
SCRIPT="$HERE/refresh_watchdog.py"
TARGET="$HOME/Library/LaunchAgents/com.myles.gaffer-watchdog.plist"

# --- validate every path BEFORE writing anything ---------------------------
fail=0
[ -d "$ROOT" ]             || { echo "error: no such directory: $ROOT" >&2; fail=1; }
[ -d "$ROOT/src/gaffer" ]  || { echo "error: $ROOT is not a Gaffer checkout (no src/gaffer)" >&2; fail=1; }
[ -x "$PYTHON" ]           || { echo "error: python not executable: $PYTHON" >&2; fail=1; }
[ -f "$TEMPLATE" ]         || { echo "error: template missing: $TEMPLATE" >&2; fail=1; }
[ -f "$SCRIPT" ]           || { echo "error: watchdog missing: $SCRIPT" >&2; fail=1; }
[ "$fail" -eq 0 ]          || { echo "refusing to install with unresolved paths" >&2; exit 1; }

# The watchdog uses datetime.UTC. A 3.9 interpreter fails at import, and it
# would fail silently every 20 minutes into a log nobody reads.
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "error: $PYTHON is older than 3.11; the watchdog needs datetime.UTC" >&2
  exit 1
fi

# gh is how the watchdog both detects a stall and dispatches the rescue.
if ! command -v gh >/dev/null 2>&1; then
  echo "error: gh is not on PATH" >&2; exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "error: gh is not authenticated; 'gh workflow run' would fail" >&2; exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
if [ -e "$TARGET" ]; then
  echo "note: $TARGET already exists; writing $TARGET.new instead"
  TARGET="$TARGET.new"
fi

sed -e "s|__GAFFER_ROOT__|$ROOT|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__HOME__|$HOME|g" "$TEMPLATE" > "$TARGET"

echo "wrote $TARGET"
echo
echo "Next steps, in order:"
echo "  1. cat '$TARGET'                                   # read it"
echo "  2. '$PYTHON' '$SCRIPT'                             # run it by hand once"
echo "  3. launchctl load '$TARGET'                        # only when YOU are ready"
