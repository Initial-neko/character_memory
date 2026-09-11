from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import weakref

import numpy as np

from character_memory.stickers import Sticker, StickerCatalog


_SHARED_CACHE_GUARD = threading.Lock()
_SHARED_CACHES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class StickerMatch:
    sticker_id: str
    score: float
    semantic_similarity: float
    lexical_score: float


class StickerCatalogView:
    """Read-only prompt view over a full StickerCatalog.

    The full catalog remains authoritative for UI/resource validation. This view
    only limits what the model sees in one reaction turn.
    """

    def __init__(self, base: StickerCatalog, stickers: list[Sticker]):
        self.base = base
        self.stickers = stickers
        self.source = f"{base.source}:retrieved"
        self._by_id = {item.id: item for item in stickers}

    def get(self, sticker_id: str):
        return self._by_id.get(sticker_id)

    def asset_path(self, sticker_id: str):
        if sticker_id not in self._by_id:
            return None
        return self.base.asset_path(sticker_id)

    def prompt_text(self) -> str:
        if not self.stickers:
            return "- 无合适候选"
        rows = []
        for item in self.stickers:
            meaning = "、".join(item.tags) or item.description or item.label
            rows.append(f"- {item.id}: {item.label}；适合：{meaning}")
        return "\n".join(rows)


@dataclass(frozen=True)
class StickerRetrievalResult:
    catalog: StickerCatalogView
    matches: list[StickerMatch]
    query: str


class StickerRetriever:
    """Cheap local candidate retrieval for the model's Sticker working set.

    UI still sees the whole global Sticker library. Runtime gets at most `limit`
    candidates selected by metadata lexical match plus embedding similarity.
    Catalog embeddings are cached by sticker semantic text and recomputed only
    when metadata changes. Runtimes sharing one EmbeddingProvider also share this
    cache, so a group does not encode the same Sticker library once per member.
    """

    def __init__(self, embeddings, *, limit: int = 12, semantic_threshold: float = 0.46):
        self.embeddings = embeddings
        self.limit = max(1, int(limit))
        self.semantic_threshold = float(semantic_threshold)
        try:
            with _SHARED_CACHE_GUARD:
                shared = _SHARED_CACHES.get(embeddings)
                if shared is None:
                    shared = (threading.RLock(), {})
                    _SHARED_CACHES[embeddings] = shared
            self._lock, self._cache = shared
        except TypeError:
            # Minimal test/fake providers may not support weak references.
            self._lock = threading.RLock()
            self._cache: dict[str, tuple[str, list[float]]] = {}

    @staticmethod
    def semantic_text(sticker: Sticker) -> str:
        parts = [sticker.label, *sticker.tags, sticker.description, sticker.pack_name]
        return " ".join(str(value).strip() for value in parts if str(value or "").strip())

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[\s\W_]+", "", str(text or "").casefold(), flags=re.UNICODE)

    @classmethod
    def _lexical_score(cls, query: str, sticker: Sticker) -> float:
        normalized_query = cls._normalize(query)
        if not normalized_query:
            return 0.0
        score = 0.0
        label = cls._normalize(sticker.label)
        if label and (len(label) >= 2 or normalized_query == label) and label in normalized_query:
            score += 0.9
        tag_hits = 0
        for tag in sticker.tags:
            token = cls._normalize(tag)
            if not token:
                continue
            if len(token) < 2 and normalized_query != token:
                continue
            if token in normalized_query:
                tag_hits += 1
        score += min(tag_hits, 3) * 0.45
        return score

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        a = np.asarray(left, dtype=np.float32)
        b = np.asarray(right, dtype=np.float32)
        if not a.size or a.size != b.size:
            return 0.0
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom <= 1e-8:
            return 0.0
        return float(np.dot(a, b) / denom)

    def _embed_many(self, texts: list[str]) -> list[list[float]]:
        method = getattr(self.embeddings, "embed_many", None)
        if callable(method):
            return method(texts)
        return [self.embeddings.embed(text) for text in texts]

    def _catalog_vectors(self, catalog: StickerCatalog) -> dict[str, list[float]]:
        missing_ids: list[str] = []
        missing_texts: list[str] = []
        with self._lock:
            for sticker in catalog.stickers:
                text = self.semantic_text(sticker)
                cached = self._cache.get(sticker.id)
                if cached is None or cached[0] != text:
                    missing_ids.append(sticker.id)
                    missing_texts.append(text)

        if missing_texts:
            vectors = self._embed_many(missing_texts)
            with self._lock:
                for sticker_id, text, vector in zip(missing_ids, missing_texts, vectors):
                    self._cache[sticker_id] = (text, vector)

        with self._lock:
            return {
                sticker.id: self._cache[sticker.id][1]
                for sticker in catalog.stickers
                if sticker.id in self._cache
            }

    def retrieve(self, catalog: StickerCatalog | None, query: str) -> StickerRetrievalResult | None:
        if catalog is None:
            return None
        cleaned_query = str(query or "").strip()
        if not cleaned_query or not catalog.stickers:
            return StickerRetrievalResult(StickerCatalogView(catalog, []), [], cleaned_query)

        query_vector = self.embeddings.embed(cleaned_query)
        vectors = self._catalog_vectors(catalog)
        ranked: list[tuple[float, Sticker, StickerMatch]] = []
        for sticker in catalog.stickers:
            lexical = self._lexical_score(cleaned_query, sticker)
            similarity = self._cosine(query_vector, vectors.get(sticker.id, []))
            if lexical <= 0.0 and similarity < self.semantic_threshold:
                continue
            score = similarity + lexical
            match = StickerMatch(
                sticker_id=sticker.id,
                score=round(score, 4),
                semantic_similarity=round(similarity, 4),
                lexical_score=round(lexical, 4),
            )
            ranked.append((score, sticker, match))

        ranked.sort(key=lambda item: (-item[0], item[1].id))
        selected = ranked[: self.limit]
        return StickerRetrievalResult(
            catalog=StickerCatalogView(catalog, [item[1] for item in selected]),
            matches=[item[2] for item in selected],
            query=cleaned_query,
        )
