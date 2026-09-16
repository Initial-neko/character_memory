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
  echo "Open Tailscale and connect this PC to the tailnet, then run this script again." >&2
  exit 1
fi

if [[ -z "$DNS_NAME" || "$DNS_NAME" != *.ts.net ]]; then
  echo "[FAIL] Tailscale DNS name was not available from 'tailscale status --json'." >&2
  echo "MagicDNS/HTTPS must be available for the V1 mobile path." >&2
  exit 1
fi

MOBILE_ORIGIN="https://$DNS_NAME"

echo "Character Memory Mobile Start"
echo "============================="
echo "[OK] Tailscale connected"
echo "[OK] Mobile origin: $MOBILE_ORIGIN"
echo

bash scripts/tailscale-serve.sh

echo
echo "Starting Character Memory with exact Media CORS for:"
echo "  $MOBILE_ORIGIN"
echo
echo "Phone URL:"
echo "  $MOBILE_ORIGIN"
echo
echo "Validation command (run in another Git Bash after the stack is ready):"
echo "  bash scripts/mobile-check.sh"
echo

exec uv run character-stack --no-browser --mobile-origin "$MOBILE_ORIGIN" "$@"
