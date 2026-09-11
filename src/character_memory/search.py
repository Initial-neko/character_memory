from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging

import httpx


logger = logging.getLogger("character_memory.search")


@dataclass(frozen=True)
class ImageSearchResult:
    title: str
    image_url: str
    thumbnail_url: str
    source_page_url: str
    source_domain: str
    width: int | None = None
    height: int | None = None
    confidence: str = ""


@dataclass(frozen=True)
class WebSearchResult:
    """Reserved result contract for the later general-purpose web_search tool."""

    title: str
    url: str
    snippet: str
    source_domain: str
    published_at: str | None = None


@dataclass(frozen=True)
class FetchedPage:
    """Reserved result contract for the later web_fetch capability."""

    url: str
    title: str
    content: str
    content_type: str = "text/plain"


class SearchProvider(ABC):
    @abstractmethod
    def search_images(self, query: str, *, limit: int = 12) -> list[ImageSearchResult]:
        ...

    def search_web(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        raise NotImplementedError("web_search is reserved for a later phase")

    def close(self) -> None:
        pass


class WebFetcher(ABC):
    """Separate from SearchProvider on purpose: searching and fetching pages are different trust boundaries."""

    def fetch(self, url: str, *, max_chars: int = 12000) -> FetchedPage:
        raise NotImplementedError("web_fetch is reserved for a later phase")


class BraveSearchProvider(SearchProvider):
    IMAGE_SEARCH_URL = "https://api.search.brave.com/res/v1/images/search"

    def __init__(
        self,
        api_key: str,
        *,
        country: str = "ALL",
        language: str = "zh",
        safe_search: str = "strict",
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.country = str(country or "ALL").strip().upper() or "ALL"
        self.language = str(language or "zh").strip() or "zh"
        self.safe_search = str(safe_search or "strict").strip().lower() or "strict"
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def search_images(self, query: str, *, limit: int = 12) -> list[ImageSearchResult]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("image search query must not be empty")
        if not self.api_key:
            raise RuntimeError("search_api_key is empty; configure a Brave Search API key first")

        count = max(1, min(int(limit), 50))
        response = self.client.get(
            self.IMAGE_SEARCH_URL,
            params={
                "q": query,
                "count": count,
                "country": self.country,
                "search_lang": self.language,
                "safesearch": self.safe_search,
            },
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self.api_key,
                "User-Agent": "character-memory/0.4 avatar-search",
            },
        )
        if response.is_error:
            body = (response.text or "").strip()
            if len(body) > 1200:
                body = body[:1200] + "…"
            raise RuntimeError(f"Brave image search failed with HTTP {response.status_code}: {body or '<empty>'}")

        payload = response.json()
        results: list[ImageSearchResult] = []
        for item in payload.get("results") or []:
            properties = item.get("properties") or {}
            thumbnail = item.get("thumbnail") or {}
            image_url = str(properties.get("url") or "").strip()
            thumbnail_url = str(thumbnail.get("src") or properties.get("placeholder") or "").strip()
            source_page_url = str(item.get("url") or "").strip()
            if not image_url or not thumbnail_url or not source_page_url:
                continue
            meta_url = item.get("meta_url") or {}
            source_domain = str(item.get("source") or meta_url.get("hostname") or meta_url.get("netloc") or "").strip()
            width = properties.get("width")
            height = properties.get("height")
            try:
                width = int(width) if width is not None else None
                height = int(height) if height is not None else None
            except (TypeError, ValueError):
                width = height = None
            # Extremely panoramic assets make poor chat avatars. Keep unknown
            # dimensions, but reject obvious banners before they reach the UI.
            if width and height:
                ratio = width / max(height, 1)
                if ratio < 0.45 or ratio > 2.2:
                    continue
            results.append(
                ImageSearchResult(
                    title=str(item.get("title") or source_domain or "头像候选").strip(),
                    image_url=image_url,
                    thumbnail_url=thumbnail_url,
                    source_page_url=source_page_url,
                    source_domain=source_domain,
                    width=width,
                    height=height,
                    confidence=str(item.get("confidence") or "").strip(),
                )
            )
            if len(results) >= count:
                break

        logger.info("search.images provider=brave query_chars=%d returned=%d", len(query), len(results))
        return results

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
