#!/usr/bin/env bash
# Qwen3-TTS 1.7B VoiceDesign tool sidecar (:9015).
#
# This is a TOOL, not a realtime provider -- see the module docstring in
# src/character_memory/qwen3_voice_design_experiment.py. Do not add it to
# config.yaml: tts_provider, and do not let character-stack start it: the 1.7B
# checkpoint reserves ~4.4 GB, which on an 8 GB card evicts GSV and breaks live
# chat. Start it by hand when you want to design a voice.
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
export QWEN3_VOICE_DESIGN_MODEL="${QWEN3_VOICE_DESIGN_MODEL:-Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign}"
export QWEN3_VOICE_DESIGN_PORT="${QWEN3_VOICE_DESIGN_PORT:-9015}"
export QWEN3_VOICE_DESIGN_DEVICE="${QWEN3_VOICE_DESIGN_DEVICE:-auto}"
export QWEN3_VOICE_DESIGN_DTYPE="${QWEN3_VOICE_DESIGN_DTYPE:-auto}"
export QWEN3_VOICE_DESIGN_ATTN="${QWEN3_VOICE_DESIGN_ATTN:-sdpa}"
export QWEN3_VOICE_DESIGN_LANGUAGE="${QWEN3_VOICE_DESIGN_LANGUAGE:-Chinese}"
# Lazy by default: an eager load would take the card before anyone asks for it.
export QWEN3_VOICE_DESIGN_PRELOAD="${QWEN3_VOICE_DESIGN_PRELOAD:-0}"

exec "$PY" -m character_memory.qwen3_voice_design_experiment "$@"
