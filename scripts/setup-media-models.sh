#!/usr/bin/env bash
set -euo pipefail

# This file is intentionally LF-only. See .gitattributes.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_ROOT="${CHARACTER_MEDIA_MODEL_ROOT:-$ROOT/models}"
ASR_NAME="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
TTS_NAME="sherpa-onnx-vits-zh-ll"
KOKORO_NAME="kokoro-82m-v1.1-zh"
KOKORO_DIR="${CHARACTER_TTS_KOKORO_MODEL_DIR:-$MODEL_ROOT/$KOKORO_NAME}"
KOKORO_BASE_URL="https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/resolve/main"
KOKORO_VOICES=(zf_001 zf_002 zf_003 zf_004)
ASR_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${ASR_NAME}.tar.bz2"
TTS_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${TTS_NAME}.tar.bz2"

mkdir -p "$MODEL_ROOT" "$KOKORO_DIR/voices"

for cmd in curl tar; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[error] required command '$cmd' was not found in this shell." >&2
    exit 1
  fi
done

curl_download() {
  local url="$1"
  local dest="$2"
  local tmp="${dest}.part"
  local args=(-fL --retry 3 --retry-delay 2)

  if [[ -n "${HF_TOKEN:-}" && "$url" == https://huggingface.co/* ]]; then
    args+=(-H "Authorization: Bearer $HF_TOKEN")
  fi

  mkdir -p "$(dirname "$dest")"
  echo "[download] $dest"
  curl "${args[@]}" -o "$tmp" "$url"
  mv -f "$tmp" "$dest"
}

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

  curl_download "$url" "$archive"
  echo "[extract] $archive"
  tar -xjf "$archive" -C "$MODEL_ROOT"
  rm -f "$archive"

  if [[ ! -f "$dir/$required" ]]; then
    echo "[error] expected $dir/$required after extraction" >&2
    exit 1
  fi
}

fetch_file() {
  local relative="$1"
  local dest="$KOKORO_DIR/$relative"
  if [[ -f "$dest" ]]; then
    echo "[ok] $KOKORO_NAME/$relative already exists"
    return
  fi
  curl_download "$KOKORO_BASE_URL/$relative" "$dest"
}

fetch_model "$ASR_NAME" "$ASR_URL" "model.int8.onnx"
fetch_model "$TTS_NAME" "$TTS_URL" "model.onnx"

# Kokoro must be fully available before :9002 starts synthesizing. Do not let
# request-time code download model/voice files from Hugging Face.
fetch_file "config.json"
fetch_file "kokoro-v1_1-zh.pth"
for voice in "${KOKORO_VOICES[@]}"; do
  fetch_file "voices/${voice}.pt"
done

cat <<EOF

Media/TTS models are ready under:
  $MODEL_ROOT

Kokoro model:
  $KOKORO_DIR

Available Kokoro audition voices:
  ${KOKORO_VOICES[*]}

Start the stack with:
  uv run character-stack --open tts
EOF
