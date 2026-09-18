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
TORCH_BACKEND="${QWEN3_TTS_TORCH_BACKEND:-cu128}"
TORCH_VERSION="${QWEN3_TTS_TORCH_VERSION:-2.9.1}"
PREFETCH=0
CPU_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefetch) PREFETCH=1; shift ;;
    --cpu) CPU_ONLY=1; shift ;;
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

if [[ "$CPU_ONLY" == "1" ]]; then
  echo "Installing CPU PyTorch for Qwen3-TTS..."
  uv pip uninstall --python "$PY" torch torchaudio >/dev/null 2>&1 || true
  uv pip install --python "$PY" "torch==$TORCH_VERSION" "torchaudio==$TORCH_VERSION"
  echo "Installing isolated Qwen3-TTS dependencies..."
  uv pip install --python "$PY" -r scripts/qwen3-tts-requirements.txt
else
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "[FAIL] NVIDIA GPU/driver not detected (nvidia-smi missing)." >&2
    echo "Use --cpu only when CPU inference is intentionally required." >&2
    exit 1
  fi

  echo "Installing pinned CUDA PyTorch:"
  echo "  torch=$TORCH_VERSION / torchaudio=$TORCH_VERSION"
  echo "  backend=$TORCH_BACKEND"
  # uv has a dedicated PyTorch backend resolver. Use the base package version
  # and let uv select the platform-correct CUDA wheel instead of encoding the
  # +cu128 local-version suffix ourselves.
  uv pip uninstall --python "$PY" torch torchaudio >/dev/null 2>&1 || true
  uv pip install --python "$PY" "torch==$TORCH_VERSION" "torchaudio==$TORCH_VERSION" --torch-backend "$TORCH_BACKEND"

  echo "Installing isolated Qwen3-TTS dependencies without losing CUDA Torch..."
  uv pip install --python "$PY" -r scripts/qwen3-tts-requirements.txt --torch-backend "$TORCH_BACKEND"
fi

echo
echo "Environment check:"
QWEN3_TTS_EXPECT_CUDA="$((1 - CPU_ONLY))" "$PY" - <<'PY'
import os
import torch
import qwen_tts

expect_cuda = os.getenv("QWEN3_TTS_EXPECT_CUDA") == "1"
print("  qwen_tts: OK")
print("  torch:", torch.__version__)
print("  torch_cuda:", torch.version.cuda)
print("  cuda_available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("  gpu:", torch.cuda.get_device_name(0))
    props = torch.cuda.get_device_properties(0)
    print("  vram_gb:", round(props.total_memory / (1024 ** 3), 2))
elif expect_cuda:
    raise SystemExit(
        "[FAIL] NVIDIA GPU was requested but CUDA PyTorch is still unavailable. "
        "Do not continue to benchmark; delete .venv-qwen3-tts and rerun setup."
    )
else:
    print("  CPU-only mode explicitly selected.")
PY

export HF_HOME="${HF_HOME:-$ROOT/models/huggingface}"
if [[ "$PREFETCH" == "1" ]]; then
  echo
  echo "Prefetching: $MODEL"
  "$PY" scripts/prefetch_qwen3_tts.py --model "$MODEL"
fi

echo
echo "Qwen3-TTS experiment is ready."
echo "Start:     bash scripts/start-qwen3-tts.sh --device cuda:0 --dtype float16"
echo "Benchmark: bash scripts/benchmark-qwen3-tts.sh --repeats 10"
