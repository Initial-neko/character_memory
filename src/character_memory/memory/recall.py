from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from character_memory.memory.embedding import EmbeddingProvider


class VectorRecall:
    """V0 baseline: semantic similarity + recency + importance."""

    def __init__(self, store, embeddings: EmbeddingProvider, limit: int = 8):
        self.store = store
        self.embeddings = embeddings
        self.limit = limit

    def recall(self, character_id: str, query: str, limit: int | None = None, now: datetime | None = None):
        q = np.asarray(self.embeddings.embed(query), dtype=np.float32)
        now = now or datetime.now(timezone.utc)
        n = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        scored = []
        for memory in self.store.list_memories(character_id):
            if not memory.embedding:
                continue
            t = memory.event_time if memory.event_time.tzinfo else memory.event_time.replace(tzinfo=timezone.utc)
            if t > n:  # simulated future memories must never leak backward in time
                continue
            v = np.asarray(memory.embedding, dtype=np.float32)
            if v.size != q.size:
                continue  # use `character-memory reembed` after changing embedding models
            semantic = float(np.dot(q, v) / (np.linalg.norm(q) * np.linalg.norm(v) + 1e-8))
            days = max(0.0, (n - t).total_seconds() / 86400)
            recency = 1 / (1 + days / 30)
            score = 0.70 * semantic + 0.20 * recency + 0.10 * memory.importance
            scored.append((score, memory))
        return [m for _, m in sorted(scored, key=lambda item: item[0], reverse=True)[: (limit or self.limit)]]
