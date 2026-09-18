#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VENV="${QWEN3_TTS_VENV:-$ROOT/.venv-qwen3-tts}"

if [[ -x "$VENV/Scripts/python.exe" ]]; then
  PY="$VENV/Scripts/python.exe"
elif [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
else
  echo "[FAIL] Qwen3-TTS isolated environment not found: $VENV" >&2
  echo "Run: bash scripts/setup-qwen3-tts.sh --prefetch" >&2
  exit 1
fi

export HF_HOME="${HF_HOME:-$ROOT/models/huggingface}"
export PYTHONPATH="$ROOT/src"
export QWEN3_TTS_MODEL="${QWEN3_TTS_MODEL:-Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice}"
export QWEN3_TTS_DEVICE="${QWEN3_TTS_DEVICE:-auto}"
export QWEN3_TTS_DTYPE="${QWEN3_TTS_DTYPE:-auto}"
export QWEN3_TTS_ATTN="${QWEN3_TTS_ATTN:-sdpa}"

exec "$PY" -m character_memory.qwen3_tts_experiment "$@"
