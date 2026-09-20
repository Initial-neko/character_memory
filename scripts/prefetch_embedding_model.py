from __future__ import annotations

import argparse
import os
from pathlib import Path


DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    os.environ.setdefault("HF_HOME", str(root / "models" / "huggingface"))
    parser = argparse.ArgumentParser(description="Prefetch Character Memory's local embedding model.")
    parser.add_argument("--model", default=os.getenv("CHARACTER_EMBEDDING_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()

    from sentence_transformers import SentenceTransformer

    print(f"[embedding] prefetch {args.model}")
    model = SentenceTransformer(args.model)
    vector = model.encode("本地向量模型预加载验证", normalize_embeddings=True)
    print(f"[embedding] ready model={args.model} dims={len(vector)}")


if __name__ == "__main__":
    main()
