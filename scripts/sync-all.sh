#!/usr/bin/env bash
set -euo pipefail

# Canonical Character Memory development-environment sync.
#
# Important: `uv sync` is exact by default. Running a single extra such as
# `uv sync --extra image-generation` tells uv that the target environment is
# base dependencies + that extra, so packages belonging only to api/media/etc.
# are removed as extraneous.
#
# Keep one stable target instead: the project's `all` extra contains the
# complete local development/runtime stack (API, embeddings, Media Runtime,
# ImageGen, UI and pytest). Re-running this command is incremental; uv only
# changes packages when the declared dependency graph changes. The project now
# commits a verified uv.lock, so the sync below runs locked and transitive
# dependencies cannot drift.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "sync-all: uv is not available on PATH" >&2
  exit 127
fi

SYNC_ARGS=(--extra all)
if [[ -f uv.lock ]]; then
  SYNC_ARGS+=(--locked)
  echo "sync-all: verified uv.lock found; using locked dependency graph"
else
  echo "sync-all: uv.lock is not committed/present; resolving declared dependency graph"
fi

echo "sync-all: syncing canonical development stack"
uv sync "${SYNC_ARGS[@]}" "$@"
echo "sync-all: ready"
