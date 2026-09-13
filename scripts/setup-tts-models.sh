#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "setup-tts-models: uv is not available on PATH" >&2
  exit 127
fi

echo "setup-tts-models: syncing canonical stack"
bash scripts/sync-all.sh

echo "setup-tts-models: downloading TTS model assets"
export HF_HOME="${HF_HOME:-$ROOT/models/huggingface}"
uv run python scripts/prefetch_tts_models.py

echo "setup-tts-models: ready"
