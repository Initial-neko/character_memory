#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FAILURES=0

ok() { echo "[OK] $*"; }
warn() { echo "[WARN] $*"; }
fail() { echo "[FAIL] $*" >&2; FAILURES=$((FAILURES + 1)); }

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

if ! command -v curl >/dev/null 2>&1; then
  echo "[FAIL] curl not found in PATH." >&2
  exit 1
fi

STATUS_JSON="$($TAILSCALE status --json)"
BACKEND_STATE="$(printf '%s' "$STATUS_JSON" | uv run python -c 'import json,sys; print(json.load(sys.stdin).get("BackendState", ""))')"
DNS_NAME="$(printf '%s' "$STATUS_JSON" | uv run python -c 'import json,sys; print((json.load(sys.stdin).get("Self", {}).get("DNSName") or "").rstrip("."))')"
MOBILE_PEERS="$(printf '%s' "$STATUS_JSON" | uv run python -c 'import json,sys; d=json.load(sys.stdin); p=d.get("Peer", {}) or {}; print(", ".join(sorted(str(v.get("HostName") or v.get("DNSName") or "mobile") for v in p.values() if str(v.get("OS", "")).lower() in {"android", "ios"} and v.get("Online") is True)))')"

if [[ "$BACKEND_STATE" == "Running" ]]; then
  ok "Tailscale connected"
else
  fail "Tailscale is not connected (BackendState=$BACKEND_STATE)"
fi

if [[ -n "$DNS_NAME" && "$DNS_NAME" == *.ts.net ]]; then
  MOBILE_ORIGIN="https://$DNS_NAME"
  ok "Tailscale HTTPS hostname: $MOBILE_ORIGIN"
else
  MOBILE_ORIGIN=""
  fail "Tailscale *.ts.net DNS name is unavailable"
fi

if [[ -n "$MOBILE_PEERS" ]]; then
  ok "Online mobile peer visible: $MOBILE_PEERS"
else
  warn "No online Android/iOS peer is currently visible in tailscale status"
fi

SERVE_STATUS="$($TAILSCALE serve status 2>&1 || true)"
if printf '%s' "$SERVE_STATUS" | grep -Fq "127.0.0.1:8000"; then
  ok "Serve routes Character Runtime :443 -> :8000"
else
  fail "Serve mapping to Character Runtime :8000 was not found"
fi
if printf '%s' "$SERVE_STATUS" | grep -Fq "127.0.0.1:8001"; then
  ok "Serve routes Media Runtime :8443 -> :8001"
else
  fail "Serve mapping to Media Runtime :8001 was not found"
fi

check_url() {
  local label="$1"
  local url="$2"
  if curl -fsS --max-time 6 "$url" >/dev/null; then
    ok "$label"
  else
    fail "$label ($url)"
  fi
}

check_url "Local Character Runtime healthy" "http://127.0.0.1:8000/health"
check_url "Local Media Runtime healthy" "http://127.0.0.1:8001/health"

if [[ -n "$MOBILE_ORIGIN" ]]; then
  CORS_HEADERS="$(curl -sS -D - -o /dev/null --max-time 6 -H "Origin: $MOBILE_ORIGIN" "http://127.0.0.1:8001/health" 2>/dev/null || true)"
  if printf '%s' "$CORS_HEADERS" | tr -d '\r' | grep -Fqi "access-control-allow-origin: $MOBILE_ORIGIN"; then
    ok "Media CORS allows exact mobile origin"
  else
    fail "Media CORS does not allow $MOBILE_ORIGIN; restart with scripts/mobile-start.sh"
  fi

  check_url "Remote Character HTTPS healthy" "$MOBILE_ORIGIN/health"
  check_url "Remote Media HTTPS healthy" "$MOBILE_ORIGIN:8443/health"
fi

echo
if [[ "$FAILURES" -eq 0 ]]; then
  echo "Mobile access validation: PASS"
  if [[ -n "$MOBILE_ORIGIN" ]]; then
    echo "Phone URL: $MOBILE_ORIGIN"
  fi
  exit 0
fi

echo "Mobile access validation: FAIL ($FAILURES check(s))" >&2
echo "Run 'tailscale serve status' and see docs/current/MOBILE_ACCESS.md for targeted troubleshooting." >&2
exit 1
