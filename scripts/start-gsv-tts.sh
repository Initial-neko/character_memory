#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GSV_ROOT="${GSV_TTS_ROOT:-$ROOT/.external/GSV-TTS-Lite}"
VENV="${GSV_TTS_VENV:-$GSV_ROOT/.venv}"

if [[ -x "$VENV/Scripts/python.exe" ]]; then
  PY="$VENV/Scripts/python.exe"
  if command -v cygpath >/dev/null 2>&1; then
    ROOT_SRC="$(cygpath -w "$ROOT/src")"
    GSV_PY_ROOT="$(cygpath -w "$GSV_ROOT")"
  else
    ROOT_SRC="$ROOT/src"
    GSV_PY_ROOT="$GSV_ROOT"
  fi
  export PYTHONPATH="$ROOT_SRC;$GSV_PY_ROOT${PYTHONPATH:+;$PYTHONPATH}"
elif [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
  export PYTHONPATH="$ROOT/src:$GSV_ROOT${PYTHONPATH:+:$PYTHONPATH}"
else
  echo "[FAIL] GSV-TTS-Lite environment not found: $VENV" >&2
  echo "Expected the already validated environment under .external/GSV-TTS-Lite/.venv" >&2
  exit 1
fi

if [[ ! -d "$GSV_ROOT/gsv_tts" ]]; then
  echo "[FAIL] GSV-TTS-Lite source not found: $GSV_ROOT" >&2
  exit 1
fi

# Per-persona voice manifests live beside their persona.yaml. The sidecar globs
# this, so it must be absolute: a relative glob resolves against whatever cwd the
# process happens to start in.
PERSONA_ROOT="$ROOT/personas"
if command -v cygpath >/dev/null 2>&1; then
  PERSONA_ROOT="$(cygpath -w "$PERSONA_ROOT")"
fi

export GSV_TTS_HOST="${GSV_TTS_HOST:-127.0.0.1}"
export GSV_TTS_PORT="${GSV_TTS_PORT:-9014}"
export GSV_TTS_DEVICE="${GSV_TTS_DEVICE:-cuda}"
export GSV_TTS_PRELOAD="${GSV_TTS_PRELOAD:-1}"
export GSV_TTS_VOICE="${GSV_TTS_VOICE:-murasame}"
export GSV_TTS_LANGUAGE="${GSV_TTS_LANGUAGE:-zh}"
export GSV_TTS_PROMPT_LANGUAGE="${GSV_TTS_PROMPT_LANGUAGE:-auto}"
export GSV_TTS_PERSONA_ROOT="${GSV_TTS_PERSONA_ROOT:-$PERSONA_ROOT}"

for name in GSV_TTS_GPT_MODEL GSV_TTS_SOVITS_MODEL GSV_TTS_REF_AUDIO GSV_TTS_REF_TEXT; do
  if [[ -z "${!name:-}" ]]; then
    echo "[WARN] $name is not set; sidecar will start but /health will report not ready." >&2
  fi
done

exec "$PY" -m character_memory.gsv_tts_experiment "$@"
