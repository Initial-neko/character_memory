from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import hf_hub_download


ROOT = Path(__file__).resolve().parents[1]
REPO_ID = "hexgrad/Kokoro-82M-v1.1-zh"
DEFAULT_VOICES = ("zf_001", "zf_002", "zf_003", "zf_004")
FILES = (
    "config.json",
    "kokoro-v1_1-zh.pth",
    *(f"voices/{voice}.pt" for voice in DEFAULT_VOICES),
)


def main() -> None:
    hf_home = Path(os.getenv("HF_HOME", ROOT / "models" / "huggingface")).resolve()
    hub_cache = hf_home / "hub"
    hub_cache.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)

    print(f"tts-models: HF_HOME={hf_home}")
    print(f"tts-models: prefetching {REPO_ID}")
    if not os.getenv("HF_TOKEN"):
        print("tts-models: HF_TOKEN is not set; public download will still work but may be rate-limited")

    resolved: list[Path] = []
    for filename in FILES:
        print(f"tts-models: downloading {filename} ...", flush=True)
        path = hf_hub_download(
            repo_id=REPO_ID,
            filename=filename,
            cache_dir=str(hub_cache),
            token=os.getenv("HF_TOKEN") or None,
        )
        resolved.append(Path(path))

    missing = [str(path) for path in resolved if not path.is_file()]
    if missing:
        raise SystemExit(f"tts-models: download completed but files are missing: {missing}")

    print("tts-models: Kokoro model and audition voices are ready")
    print("tts-models: voices=" + ",".join(DEFAULT_VOICES))


if __name__ == "__main__":
    main()
