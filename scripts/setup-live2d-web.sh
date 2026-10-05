#!/usr/bin/env bash
# Usage: bash scripts/setup-live2d-web.sh /path/to/CubismSdkForWeb/Core/live2dcubismcore.min.js
# The proprietary Cubism Core must be obtained separately from Live2D.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE="${1:-}"
if [[ -z "$CORE" || ! -f "$CORE" ]]; then
  echo "Provide the path to a licensed Cubism 5 SDK for Web Core JS file." >&2
  exit 2
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to prepare the optional renderer." >&2
  exit 2
fi
DEPS="$ROOT/.external/live2d-web-deps"
TARGET="$ROOT/src/character_memory/web/vendor/live2d"
mkdir -p "$DEPS" "$TARGET"
npm install --prefix "$DEPS" --ignore-scripts --no-audit --no-fund --no-save \
  pixi.js@8.13.1 untitled-pixi-live2d-engine@1.4.0
cp "$DEPS/node_modules/pixi.js/dist/pixi.min.js" "$TARGET/pixi.min.js"
cp "$DEPS/node_modules/untitled-pixi-live2d-engine/dist/cubism.js" "$TARGET/cubism.js"
cp "$CORE" "$TARGET/live2dcubismcore.min.js"
echo "Live2D Web renderer prepared locally at $TARGET (ignored by git)."
