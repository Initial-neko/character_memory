#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "setup-tts-models: reusing scripts/setup-media-models.sh"
exec bash scripts/setup-media-models.sh "$@"
