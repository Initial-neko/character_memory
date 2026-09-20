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

        memories = []
        vectors = []
        recencies = []
        importances = []
        candidate_loader = getattr(self.store, "list_memory_candidates", None)
        if callable(candidate_loader):
            source_memories = candidate_loader(character_id, at=n)
        else:
            source_memories = self.store.list_memories(character_id)
        for memory in source_memories:
            if not memory.embedding:
                continue
            t = memory.event_time if memory.event_time.tzinfo else memory.event_time.replace(tzinfo=timezone.utc)
            if t > n:  # simulated future memories must never leak backward in time
                continue
            if len(memory.embedding) != q.size:
                continue  # use `character-memory reembed` after changing embedding models
            memories.append(memory)
            vectors.append(memory.embedding)
            days = max(0.0, (n - t).total_seconds() / 86400)
            recencies.append(1 / (1 + days / 30))
            importances.append(memory.importance)

        if not memories:
            return []

        matrix = np.asarray(vectors, dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        row_norms = np.linalg.norm(matrix, axis=1)
        semantic = (matrix @ q) / (row_norms * q_norm + 1e-8)
        scores = (
            0.70 * semantic
            + 0.20 * np.asarray(recencies, dtype=np.float32)
            + 0.10 * np.asarray(importances, dtype=np.float32)
        )
        count = min(limit or self.limit, len(memories))
        order = np.argsort(-scores, kind="stable")[:count]
        return [memories[int(index)] for index in order]
