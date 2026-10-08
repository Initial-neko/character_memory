#!/usr/bin/env bash
# Start / stop / restart / inspect the local Character Memory stack.
#
# This is a thin wrapper: the launcher itself owns the process handling. It
# exclusively claims .debug-output/stack.pid while it runs and shuts children
# down in reverse order when it sees .debug-output/stack.stop, so `--stop` does not have
# to guess at a process tree from the outside.
#
#   bash scripts/stack.sh status
#   bash scripts/stack.sh start [-- extra launcher args]
#   bash scripts/stack.sh stop
#   bash scripts/stack.sh restart
#
# The underlying commands, if you would rather type them:
#
#   uv run character-stack --status
#   uv run character-stack --stop
#   uv run character-stack --no-browser
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "[FAIL] uv not found in PATH." >&2
  exit 1
fi

command="${1:-status}"
shift || true
if [ "${1:-}" = "--" ]; then shift; fi

case "$command" in
  status) exec uv run character-stack --status ;;
  stop) exec uv run character-stack --stop ;;
  start) exec uv run character-stack "$@" ;;
  restart)
    uv run character-stack --stop
    exec uv run character-stack "$@"
    ;;
  "" | -h | --help | help)
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "unknown command: $command" >&2
    echo "usage: bash scripts/stack.sh [status|start|stop|restart] [-- extra args]" >&2
    exit 2
    ;;
esac
