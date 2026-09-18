#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "[FAIL] uv not found in PATH." >&2
  exit 1
fi

VENV="${QWEN3_TTS_VENV:-$ROOT/.venv-qwen3-tts}"
MODEL="${QWEN3_TTS_MODEL:-Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice}"
PREFETCH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefetch) PREFETCH=1; shift ;;
    --model)
      [[ $# -ge 2 ]] || { echo "[FAIL] --model requires a value" >&2; exit 2; }
      MODEL="$2"; shift 2 ;;
    *) echo "[FAIL] unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -x "$VENV/Scripts/python.exe" ]]; then
  PY="$VENV/Scripts/python.exe"
elif [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
else
  echo "Creating isolated Qwen3-TTS Python 3.12 environment: $VENV"
  uv venv "$VENV" --python 3.12
  if [[ -x "$VENV/Scripts/python.exe" ]]; then
    PY="$VENV/Scripts/python.exe"
  else
    PY="$VENV/bin/python"
  fi
fi

echo "Installing isolated Qwen3-TTS dependencies..."
uv pip install --python "$PY" -r scripts/qwen3-tts-requirements.txt

echo
echo "Environment check:"
"$PY" - <<'PY'
import torch
import qwen_tts
print("  qwen_tts: OK")
print("  torch:", torch.__version__)
print("  cuda_available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("  gpu:", torch.cuda.get_device_name(0))
    props = torch.cuda.get_device_properties(0)
    print("  vram_gb:", round(props.total_memory / (1024 ** 3), 2))
else:
    print("  WARNING: CUDA is unavailable. The sidecar will fall back to CPU unless --device cuda:0 is requested, which will fail fast.")
PY

export HF_HOME="${HF_HOME:-$ROOT/models/huggingface}"
if [[ "$PREFETCH" == "1" ]]; then
  echo
  echo "Prefetching: $MODEL"
  "$PY" scripts/prefetch_qwen3_tts.py --model "$MODEL"
fi

echo
echo "Qwen3-TTS experiment is ready."
echo "Start:     bash scripts/start-qwen3-tts.sh --preload"
echo "Benchmark: uv run python scripts/benchmark_qwen3_tts.py --repeats 10"
