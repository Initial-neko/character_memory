#!/usr/bin/env bash
set -euo pipefail

# This file is intentionally LF-only. See .gitattributes; CRLF breaks bash/WSL
# by turning `pipefail` into `pipefail\r`.
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
  "$TTS_DIR/phone.fst"
  "$TTS_DIR/number.fst"
)
for file in "${required[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "[error] missing $file" >&2
    echo "Run: bash scripts/setup-media-models.sh" >&2
    exit 1
  fi
done
if [[ ! -d "$TTS_DIR/dict" ]]; then
  echo "[error] missing $TTS_DIR/dict" >&2
  echo "Run: bash scripts/setup-media-models.sh" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[error] uv was not found in this shell." >&2
  echo "Run this script from the same Git Bash/WSL environment where 'uv --version' works." >&2
  exit 1
fi

# Git Bash/MSYS reports project paths as /c/Users/..., but sherpa-onnx's native
# Windows code expects Windows paths. Keep POSIX paths for bash file checks and
# convert only the values exported to Python/native code. cygpath -m uses
# C:/Users/... so comma-separated FST lists do not need backslash escaping.
to_native_path() {
  local value="$1"
  case "$(uname -s 2>/dev/null || true)" in
    MINGW*|MSYS*|CYGWIN*)
      if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$value"
        return
      fi
      ;;
  esac
  printf '%s\n' "$value"
}

ASR_MODEL_NATIVE="$(to_native_path "$ASR_DIR/model.int8.onnx")"
ASR_TOKENS_NATIVE="$(to_native_path "$ASR_DIR/tokens.txt")"
TTS_MODEL_NATIVE="$(to_native_path "$TTS_DIR/model.onnx")"
TTS_TOKENS_NATIVE="$(to_native_path "$TTS_DIR/tokens.txt")"
TTS_LEXICON_NATIVE="$(to_native_path "$TTS_DIR/lexicon.txt")"
TTS_DICT_NATIVE="$(to_native_path "$TTS_DIR/dict")"
TTS_PHONE_FST_NATIVE="$(to_native_path "$TTS_DIR/phone.fst")"
TTS_NUMBER_FST_NATIVE="$(to_native_path "$TTS_DIR/number.fst")"

export CHARACTER_MEDIA_ASR_MODEL="$ASR_MODEL_NATIVE"
export CHARACTER_MEDIA_ASR_TOKENS="$ASR_TOKENS_NATIVE"
export CHARACTER_MEDIA_ASR_DEVICE="${CHARACTER_MEDIA_ASR_DEVICE:-cpu}"
export CHARACTER_MEDIA_ASR_THREADS="${CHARACTER_MEDIA_ASR_THREADS:-2}"
export CHARACTER_MEDIA_ASR_LANGUAGE="${CHARACTER_MEDIA_ASR_LANGUAGE:-auto}"

export CHARACTER_MEDIA_TTS_MODEL="$TTS_MODEL_NATIVE"
export CHARACTER_MEDIA_TTS_TOKENS="$TTS_TOKENS_NATIVE"
export CHARACTER_MEDIA_TTS_LEXICON="$TTS_LEXICON_NATIVE"
export CHARACTER_MEDIA_TTS_DICT_DIR="$TTS_DICT_NATIVE"
export CHARACTER_MEDIA_TTS_RULE_FSTS="$TTS_PHONE_FST_NATIVE,$TTS_NUMBER_FST_NATIVE"
export CHARACTER_MEDIA_TTS_DEVICE="${CHARACTER_MEDIA_TTS_DEVICE:-cpu}"
export CHARACTER_MEDIA_TTS_THREADS="${CHARACTER_MEDIA_TTS_THREADS:-2}"

export CHARACTER_MEDIA_HOST="${CHARACTER_MEDIA_HOST:-127.0.0.1}"
export CHARACTER_MEDIA_PORT="${CHARACTER_MEDIA_PORT:-8001}"

cat <<EOF
Character Media Runtime
  ASR: ${CHARACTER_MEDIA_ASR_DEVICE}, threads=${CHARACTER_MEDIA_ASR_THREADS}
  TTS: ${CHARACTER_MEDIA_TTS_DEVICE}, threads=${CHARACTER_MEDIA_TTS_THREADS}
  URL: http://${CHARACTER_MEDIA_HOST}:${CHARACTER_MEDIA_PORT}
  ASR model: ${CHARACTER_MEDIA_ASR_MODEL}
  TTS model: ${CHARACTER_MEDIA_TTS_MODEL}
EOF

exec uv run character-media
