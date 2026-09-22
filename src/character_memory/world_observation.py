from __future__ import annotations

import logging
from urllib.parse import urlparse

from character_memory.domain.models import WorldObservation
from character_memory.search import SearchProvider, WebFetcher


logger = logging.getLogger("character_memory.world_observation")


class WorldObservationService:
    """Discover public pages, then render them through the formal headless browser."""

    def __init__(self, search_provider: SearchProvider | None, web_fetcher: WebFetcher):
        self.search_provider = search_provider
        self.web_fetcher = web_fetcher

    def observe(
        self,
        query: str,
        *,
        max_pages: int = 2,
        max_chars_per_page: int = 6000,
        search_limit: int = 5,
    ) -> dict:
        normalized = " ".join(str(query or "").split()).strip()
        if not normalized:
            raise ValueError("world observation query must not be empty")
        if self.search_provider is None:
            raise RuntimeError("web search provider is unavailable")

        max_pages = max(1, min(4, int(max_pages)))
        search_limit = max(max_pages, min(10, int(search_limit)))
        candidates = self.search_provider.search_web(normalized, limit=search_limit)
        urls: list[str] = []
        by_url = {}
        for item in candidates:
            url = str(item.url or "").strip()
            if not url or url in by_url:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            by_url[url] = item
            urls.append(url)
            if len(urls) >= max_pages:
                break

        if not urls:
            return {"query": normalized, "observations": [], "errors": [], "search_results": 0}

        fetch_many = getattr(self.web_fetcher, "fetch_many", None)
        if callable(fetch_many):
            pages, errors = fetch_many(urls, max_chars=max_chars_per_page)
        else:
            pages = []
            errors = []
            for url in urls:
                try:
                    pages.append(self.web_fetcher.fetch(url, max_chars=max_chars_per_page))
                except Exception as exc:
                    errors.append({"url": url, "error": str(exc)[:800]})

        observations: list[WorldObservation] = []
        for page in pages:
            search = by_url.get(page.url)
            if search is None:
                search = next(
                    (
                        item for item in candidates
                        if (urlparse(item.url).hostname or "") == (urlparse(page.url).hostname or "")
                    ),
                    None,
                )
            observations.append(
                WorldObservation(
                    title=page.title or (search.title if search is not None else ""),
                    url=page.url,
                    source_domain=urlparse(page.url).hostname or "",
                    snippet=search.snippet if search is not None else page.description,
                    content=page.content,
                    published_at=search.published_at if search is not None else None,
                )
            )

        logger.info(
            "world.observe query_chars=%d search_results=%d rendered=%d errors=%d",
            len(normalized),
            len(candidates),
            len(observations),
            len(errors),
        )
        return {
            "query": normalized,
            "observations": observations,
            "errors": errors,
            "search_results": len(candidates),
        }
