#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_ROOT="${CHARACTER_MEDIA_MODEL_ROOT:-$ROOT/models}"
ASR_NAME="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
TTS_NAME="sherpa-onnx-vits-zh-ll"
ASR_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${ASR_NAME}.tar.bz2"
TTS_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${TTS_NAME}.tar.bz2"

mkdir -p "$MODEL_ROOT"

fetch_model() {
  local name="$1"
  local url="$2"
  local required="$3"
  local dir="$MODEL_ROOT/$name"
  local archive="$MODEL_ROOT/$name.tar.bz2"

  if [[ -f "$dir/$required" ]]; then
    echo "[ok] $name already exists"
    return
  fi

  echo "[download] $name"
  curl -fL --retry 3 --retry-delay 2 -o "$archive" "$url"
  echo "[extract] $archive"
  tar -xjf "$archive" -C "$MODEL_ROOT"
  rm -f "$archive"

  if [[ ! -f "$dir/$required" ]]; then
    echo "[error] expected $dir/$required after extraction" >&2
    exit 1
  fi
}

fetch_model "$ASR_NAME" "$ASR_URL" "model.int8.onnx"
fetch_model "$TTS_NAME" "$TTS_URL" "model.onnx"

cat <<EOF

Media models are ready under:
  $MODEL_ROOT

Start the CPU baseline with:
  bash scripts/run-media.sh

Then in another terminal run Character Runtime as usual and open:
  http://127.0.0.1:8000
EOF
