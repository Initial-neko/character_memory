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

# Templates live outside the persona tree but are globbed the same way, so their
# root gets the same treatment: absolute, and Windows-style for the sidecar.
VOICES_ROOT="$ROOT/voices"
if command -v cygpath >/dev/null 2>&1; then
  VOICES_ROOT="$(cygpath -w "$VOICES_ROOT")"
fi
export GSV_TTS_VOICES_ROOT="${GSV_TTS_VOICES_ROOT:-$VOICES_ROOT}"

# Readiness is the sidecar's own contract (``_asset_status``): the two base
# models, plus the default template -- the reference clip lives in the template
# now, and GSV_TTS_REF_AUDIO/GSV_TTS_REF_TEXT are read by nothing, so warning
# about them sent the operator after a cause /health would never report.
missing=()
for name in GSV_TTS_GPT_MODEL GSV_TTS_SOVITS_MODEL; do
  [[ -n "${!name:-}" ]] || missing+=("$name is not set")
done

template="$GSV_TTS_VOICES_ROOT/${GSV_TTS_VOICE}.yaml"
if command -v cygpath >/dev/null 2>&1; then
  # The exported root may be Windows-style; this check is a bash one.
  template="$(cygpath -u "$template")"
fi
if [[ ! -f "$template" ]]; then
  missing+=("default template $GSV_TTS_VOICE not found: $template")
fi

if (( ${#missing[@]} > 0 )); then
  echo "[WARN] sidecar will start but /health will report not ready:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
fi

exec "$PY" -m character_memory.gsv_tts_experiment "$@"
