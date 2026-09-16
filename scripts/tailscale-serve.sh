#!/usr/bin/env bash
set -euo pipefail

if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE="tailscale"
elif command -v tailscale.exe >/dev/null 2>&1; then
  TAILSCALE="tailscale.exe"
else
  echo "tailscale CLI not found in PATH" >&2
  exit 1
fi

"$TAILSCALE" serve --https=443 --bg 8000
"$TAILSCALE" serve --https=8443 --bg 8001

echo
echo "Character Memory is now served only inside your tailnet:"
"$TAILSCALE" serve status

echo
echo "Use the HTTPS :443 URL for chat. Media Runtime is available on the same hostname at :8443."
echo "Do not enable Tailscale Funnel for these ports."
