#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if ! command -v uv >/dev/null 2>&1; then
  echo "[FAIL] uv not found in PATH." >&2
  exit 1
fi
exec uv run python scripts/benchmark_qwen3_tts.py "$@"
