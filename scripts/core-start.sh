#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "[FAIL] uv not found in PATH." >&2
  exit 1
fi

CONFIG_PATH="${CHARACTER_MEMORY_CONFIG:-config.yaml}"

echo "Character Memory Core Start"
echo "==========================="
echo "Starting only Character Runtime on http://127.0.0.1:8000"
echo "Disabled in core mode: voice ASR/TTS, TTS Lab, Dev Console, Settings Center"
echo "Use 'uv run character-stack --open chat' when you need the full local stack."
echo

exec uv run python -m character_memory.cli --config "$CONFIG_PATH" web --host 127.0.0.1 --port 8000
