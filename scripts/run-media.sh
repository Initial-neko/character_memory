#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_ROOT="${CHARACTER_MEDIA_MODEL_ROOT:-$ROOT/models}"
ASR_DIR="$MODEL_ROOT/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
TTS_DIR="$MODEL_ROOT/sherpa-onnx-vits-zh-ll"

required=(
  "$ASR_DIR/model.int8.onnx"
  "$ASR_DIR/tokens.txt"
  "$TTS_DIR/model.onnx"
  "$TTS_DIR/tokens.txt"
  "$TTS_DIR/lexicon.txt"
)
for file in "${required[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "[error] missing $file" >&2
    echo "Run: bash scripts/setup-media-models.sh" >&2
    exit 1
  fi
done

export CHARACTER_MEDIA_ASR_MODEL="$ASR_DIR/model.int8.onnx"
export CHARACTER_MEDIA_ASR_TOKENS="$ASR_DIR/tokens.txt"
export CHARACTER_MEDIA_ASR_DEVICE="${CHARACTER_MEDIA_ASR_DEVICE:-cpu}"
export CHARACTER_MEDIA_ASR_THREADS="${CHARACTER_MEDIA_ASR_THREADS:-2}"
export CHARACTER_MEDIA_ASR_LANGUAGE="${CHARACTER_MEDIA_ASR_LANGUAGE:-zh}"

export CHARACTER_MEDIA_TTS_MODEL="$TTS_DIR/model.onnx"
export CHARACTER_MEDIA_TTS_TOKENS="$TTS_DIR/tokens.txt"
export CHARACTER_MEDIA_TTS_LEXICON="$TTS_DIR/lexicon.txt"
export CHARACTER_MEDIA_TTS_DICT_DIR="$TTS_DIR/dict"
export CHARACTER_MEDIA_TTS_RULE_FSTS="$TTS_DIR/phone.fst,$TTS_DIR/number.fst"
export CHARACTER_MEDIA_TTS_DEVICE="${CHARACTER_MEDIA_TTS_DEVICE:-cpu}"
export CHARACTER_MEDIA_TTS_THREADS="${CHARACTER_MEDIA_TTS_THREADS:-2}"

export CHARACTER_MEDIA_HOST="${CHARACTER_MEDIA_HOST:-127.0.0.1}"
export CHARACTER_MEDIA_PORT="${CHARACTER_MEDIA_PORT:-8001}"

cat <<EOF
Character Media Runtime
  ASR: ${CHARACTER_MEDIA_ASR_DEVICE}, threads=${CHARACTER_MEDIA_ASR_THREADS}
  TTS: ${CHARACTER_MEDIA_TTS_DEVICE}, threads=${CHARACTER_MEDIA_TTS_THREADS}
  URL: http://${CHARACTER_MEDIA_HOST}:${CHARACTER_MEDIA_PORT}
EOF

exec uv run character-media
