from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import logging
import time

import httpx
import numpy as np


logger = logging.getLogger("character_memory.embedding")


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    def close(self) -> None:
        pass


class DeterministicEmbedding(EmbeddingProvider):
    """Offline smoke-test embedding only; not semantic retrieval."""

    def __init__(self, dimensions: int = 64):
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        v = np.zeros(self.dimensions, dtype=np.float32)
        tokens = [text[i : i + 2] for i in range(max(1, len(text) - 1))]
        for token in tokens:
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "little")
            v[h % self.dimensions] += 1
        n = float(np.linalg.norm(v))
        return (v / n if n else v).tolist()


class SentenceTransformerEmbedding(EmbeddingProvider):
    """Local semantic embedding with cache-first HuggingFace resolution.

    Runtime is strict-offline: a model id such as ``BAAI/bge-small-zh-v1.5`` must
    already exist in the local HuggingFace cache. Setup/prefetch commands own
    network access; normal Character Runtime never falls back to the Hub.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        started = time.perf_counter()
        import_started = time.perf_counter()
        from sentence_transformers import SentenceTransformer

        import_ms = round((time.perf_counter() - import_started) * 1000, 1)
        source = "local-cache"
        load_started = time.perf_counter()
        try:
            self.model = SentenceTransformer(model_name, local_files_only=True)
        except Exception as exc:
            # Runtime is deliberately strict-offline. Model acquisition belongs
            # to setup/prefetch, never to Character Runtime startup or first chat.
            raise RuntimeError(
                f"Embedding model is not available in the local cache: {model_name}. "
                "Run bash scripts/setup-media-models.sh (or scripts/prefetch_embedding_model.py) "
                "while online, then start Character Memory again."
            ) from exc

        load_ms = round((time.perf_counter() - load_started) * 1000, 1)
        total_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "embedding ready model=%s source=%s import_ms=%.1f load_ms=%.1f total_ms=%.1f",
            model_name,
            source,
            import_ms,
            load_ms,
            total_ms,
        )

    def embed(self, text: str) -> list[float]:
        return self.model.encode(text, normalize_embeddings=True).astype(np.float32).tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(texts, normalize_embeddings=True)
        return vectors.astype(np.float32).tolist()


class OpenAICompatibleEmbedding(EmbeddingProvider):
    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 60):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = httpx.Client(timeout=self.timeout)

    def _request(self, value):
        response = self.client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": value},
        )
        response.raise_for_status()
        return response.json()["data"]

    def embed(self, text: str) -> list[float]:
        return self._request(text)[0]["embedding"]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        rows = self._request(texts)
        rows = sorted(rows, key=lambda item: int(item.get("index", 0)))
        if len(rows) != len(texts):
            raise RuntimeError(f"embedding provider returned {len(rows)} vectors for {len(texts)} inputs")
        return [row["embedding"] for row in rows]

    def close(self) -> None:
        self.client.close()
