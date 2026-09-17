#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE="tailscale"
elif command -v tailscale.exe >/dev/null 2>&1; then
  TAILSCALE="tailscale.exe"
else
  echo "[FAIL] Tailscale CLI not found in PATH." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[FAIL] uv not found in PATH." >&2
  exit 1
fi

STATUS_JSON="$($TAILSCALE status --json)"
BACKEND_STATE="$(printf '%s' "$STATUS_JSON" | uv run python -c 'import json,sys; print(json.load(sys.stdin).get("BackendState", ""))')"
DNS_NAME="$(printf '%s' "$STATUS_JSON" | uv run python -c 'import json,sys; print((json.load(sys.stdin).get("Self", {}).get("DNSName") or "").rstrip("."))')"

if [[ "$BACKEND_STATE" != "Running" ]]; then
  echo "[FAIL] Tailscale is not connected (BackendState=$BACKEND_STATE)." >&2
  exit 1
fi
if [[ -z "$DNS_NAME" || "$DNS_NAME" != *.ts.net ]]; then
  echo "[FAIL] Tailscale DNS name is unavailable." >&2
  exit 1
fi

MOBILE_ORIGIN="https://$DNS_NAME"
"$TAILSCALE" serve --https=443 --bg 8000

CONFIG_PATH="${CHARACTER_MEMORY_CONFIG:-config.yaml}"

echo "Character Memory Mobile Core Start"
echo "=================================="
echo "Phone URL: $MOBILE_ORIGIN"
echo "Only Character Runtime will stay resident."
echo "Unavailable in this mode: voice ASR/TTS, TTS Lab, Dev Console, Settings Center."
echo "For voice/video call use: bash scripts/mobile-start.sh"
echo

exec uv run python -m character_memory.cli --config "$CONFIG_PATH" web --host 127.0.0.1 --port 8000
