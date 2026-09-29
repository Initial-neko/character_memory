#!/usr/bin/env bash
set -euo pipefail

# This file is intentionally LF-only. See .gitattributes.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MODEL_ROOT="${CHARACTER_MEDIA_MODEL_ROOT:-$ROOT/models}"
ASR_NAME="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
STREAM_ASR_NAME="sherpa-onnx-streaming-paraformer-bilingual-zh-en"
TTS_NAME="sherpa-onnx-vits-zh-ll"
ASR_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${ASR_NAME}.tar.bz2"
STREAM_ASR_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${STREAM_ASR_NAME}.tar.bz2"
TTS_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${TTS_NAME}.tar.bz2"

if ! command -v uv >/dev/null 2>&1; then
  echo "[error] uv is not available on PATH" >&2
  exit 127
fi

# One setup entry for dependencies + all local media/TTS model assets.
echo "[sync] canonical Character Memory environment"
bash scripts/sync-all.sh

mkdir -p "$MODEL_ROOT"

for cmd in curl tar; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[error] required command '$cmd' was not found in this shell." >&2
    exit 1
  fi
done

fetch_model() {
  local name="$1"
  local url="$2"
  shift 2
  local required_files=("$@")
  local dir="$MODEL_ROOT/$name"
  local archive="$MODEL_ROOT/$name.tar.bz2"
  local complete=1

  for required in "${required_files[@]}"; do
    if [[ ! -f "$dir/$required" ]]; then
      complete=0
      break
    fi
  done
  if [[ "$complete" -eq 1 ]]; then
    echo "[ok] $name already exists"
    return
  fi

  # A previous interrupted extraction is not a valid installation. Remove the
  # partial model directory so extracting the canonical archive repairs it
  # instead of mixing old/new files.
  if [[ -d "$dir" ]]; then
    echo "[repair] removing incomplete $dir"
    rm -rf "$dir"
  fi

  echo "[download] $name"
  curl -fL --retry 3 --retry-delay 2 -o "$archive" "$url"
  echo "[extract] $archive"
  tar -xjf "$archive" -C "$MODEL_ROOT"
  rm -f "$archive"

  for required in "${required_files[@]}"; do
    if [[ ! -f "$dir/$required" ]]; then
      echo "[error] expected $dir/$required after extraction" >&2
      exit 1
    fi
  done
}

fetch_model "$ASR_NAME" "$ASR_URL" "model.int8.onnx" "tokens.txt"
fetch_model "$STREAM_ASR_NAME" "$STREAM_ASR_URL" "encoder.int8.onnx" "decoder.int8.onnx" "tokens.txt"
fetch_model "$TTS_NAME" "$TTS_URL" "model.onnx" "tokens.txt" "lexicon.txt" "phone.fst" "number.fst"
if [[ ! -d "$MODEL_ROOT/$TTS_NAME/dict" ]]; then
  echo "[error] expected $MODEL_ROOT/$TTS_NAME/dict after extraction" >&2
  exit 1
fi

# Reuse this existing model-setup entry for the :9002 Kokoro assets as well.
# The helper uses the Hugging Face cache under models/huggingface and downloads
# the model + valid v1.1 Chinese voice packs before runtime synthesis.
export HF_HOME="${HF_HOME:-$MODEL_ROOT/huggingface}"

# Runtime embedding is strict-offline. Acquire it explicitly during setup so
# Character Runtime never reaches Hugging Face on startup or first chat.
uv run python scripts/prefetch_embedding_model.py
uv run python scripts/prefetch_tts_models.py

cat <<EOF

Media/TTS setup is ready under:
  $MODEL_ROOT

Streaming ASR assets:
  $MODEL_ROOT/$STREAM_ASR_NAME/encoder.int8.onnx
  $MODEL_ROOT/$STREAM_ASR_NAME/decoder.int8.onnx
  $MODEL_ROOT/$STREAM_ASR_NAME/tokens.txt

The normal stack launcher auto-selects Paraformer streaming when these files
exist. Set CHARACTER_MEDIA_ASR_PROVIDER=sensevoice to force the batch fallback.

Start the full stack with:
  uv run character-stack --open tts
EOF
