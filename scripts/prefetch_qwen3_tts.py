from __future__ import annotations

import argparse
import os
from pathlib import Path

from huggingface_hub import snapshot_download


DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"


def main() -> None:
    parser = argparse.ArgumentParser(description="Prefetch Qwen3-TTS weights into the shared HF cache")
    parser.add_argument("--model", default=os.getenv("QWEN3_TTS_MODEL", DEFAULT_MODEL))
    parser.add_argument("--hf-home", default=os.getenv("HF_HOME", "models/huggingface"))
    args = parser.parse_args()

    hf_home = Path(args.hf_home).resolve()
    hub_cache = hf_home / "hub"
    hub_cache.mkdir(parents=True, exist_ok=True)
    print(f"Qwen3-TTS model: {args.model}")
    print(f"HF_HOME: {hf_home}")
    local = snapshot_download(repo_id=args.model, cache_dir=str(hub_cache))
    print(f"Prefetched: {local}")


if __name__ == "__main__":
    main()
