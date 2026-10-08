from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4
from urllib.parse import urlparse

from character_memory.domain.models import Event, EventType, WorldObservation
from character_memory.search import SearchProvider, WebFetcher


logger = logging.getLogger("character_memory.world_observation")

# Discovery spends a few extra page opens when the top-ranked candidates cannot
# be rendered here. Ranking order is not reachability order: a candidate that
# resolves to a blocked address, times out, or answers 5xx takes its slot with
# it, so three unreachable hits at the top ended the whole observation and the
# caller had no way to recover -- for group creation that was a hard failure,
# reproducible on every retry.
#
# The extra attempts are bounded because a failed navigation costs one full
# browser timeout. The common case is unchanged: when the top-ranked candidates
# render, exactly ``max_pages`` pages are opened, in one batch.
EXTRA_FETCH_ATTEMPTS = 2


def observation_lifecycle(event: Event, *, reading_scope: str = "WEB_PAGES", cognition: bool = False) -> dict:
    """Actual encounter time differs from publication time and persistence time."""
    return {
        "version": 1,
        "reading_scope": reading_scope,
        "reading_status": "SUCCEEDED",
        "appraisal_status": "KEPT",
        "observed_at": event.event_time.isoformat(),
        "recorded_at": datetime.now().astimezone().isoformat(),
        "cognition_status": "REQUESTED" if cognition else "NOT_REQUESTED",
        "memory_status": "PENDING" if cognition else "NOT_REQUESTED",
        "created_memory_ids": [],
    }


def retain_world_observation(store, event: Event, *, observation_key: str | None = None, runtime=None, reading_scope: str = "WEB_PAGES") -> tuple[Event, bool]:
    """Retain an already appraised result, optionally using existing cognition.

    A repeated local submission never repeats cognition. REQUESTED after a crash
    means the outcome is unknown; recovery must inspect trace rather than replay
    a provider call automatically. Upstream search/appraisal are not deduplicated.
    """
    if event.event_type != EventType.WORLD_OBSERVATION:
        raise ValueError("only WORLD_OBSERVATION can be retained")
    event = event.model_copy(update={"metadata": {
        **event.metadata,
        "observation_key": observation_key if observation_key is not None else uuid4().hex,
        "observation_lifecycle": observation_lifecycle(event, cognition=runtime is not None, reading_scope=reading_scope),
    }})
    saved, created = store.append_observation_once(event)
    if not created or runtime is None:
        return saved, created
    try:
        outcome = runtime.handle(saved, persist_event=False)
    except Exception:
        current = store.get_event(saved.id)
        life = dict(current.metadata["observation_lifecycle"])
        # A later delivery failure must not relabel already committed cognition.
        if life["cognition_status"] != "COMPLETED":
            life.update(cognition_status="FAILED", memory_status="NOT_COMPLETED")
            store.update_event_metadata(saved.id, {**current.metadata, "observation_lifecycle": life})
        raise
    current = store.get_event(saved.id)
    life = dict(current.metadata["observation_lifecycle"])
    life.update(cognition_status="COMPLETED", memory_status="EVALUATED",
                created_memory_ids=list(outcome.created_memory_ids))
    store.update_event_metadata(saved.id, {**current.metadata, "observation_lifecycle": life})
    return store.get_event(saved.id), True


def _ordered_candidates(candidates, *, limit: int) -> list[tuple[str, object]]:
    """Rank-order the search hits, one domain first, duplicates after.

    A result page can return several hits from the same site, and two slots
    spent on one domain spend one of the few page opens on a host that already
    answered. Preferring the first hit of each domain keeps the attempt budget
    spread across sources; repeats stay available when the pool is thin.
    """

    seen_urls: set[str] = set()
    seen_domains: set[str] = set()
    primary: list[tuple[str, object]] = []
    repeated: list[tuple[str, object]] = []
    for item in candidates:
        url = str(getattr(item, "url", "") or "").strip()
        if not url or url in seen_urls:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        seen_urls.add(url)
        domain = (parsed.hostname or "").lower()
        if domain and domain in seen_domains:
            repeated.append((url, item))
        else:
            seen_domains.add(domain)
            primary.append((url, item))
    return (primary + repeated)[:limit]


class WorldObservationService:
    """Discover public pages, then render them through the formal headless browser."""

    def __init__(self, search_provider: SearchProvider | None, web_fetcher: WebFetcher):
        self.search_provider = search_provider
        self.web_fetcher = web_fetcher

    def _fetch(self, batch: list[str], *, max_chars: int) -> tuple[list, list[dict[str, str]]]:
        fetch_many = getattr(self.web_fetcher, "fetch_many", None)
        if callable(fetch_many):
            return fetch_many(batch, max_chars=max_chars)
        pages = []
        errors: list[dict[str, str]] = []
        for url in batch:
            try:
                pages.append(self.web_fetcher.fetch(url, max_chars=max_chars))
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)[:800]})
        return pages, errors

    def _render(self, ordered: list[tuple[str, object]], *, wanted: int, max_chars: int):
        """Open pages until ``wanted`` render, promoting the next candidate on failure."""

        pages: list = []
        errors: list[dict[str, str]] = []
        position = 0
        while len(pages) < wanted and position < len(ordered):
            batch = [url for url, _ in ordered[position:position + (wanted - len(pages))]]
            position += len(batch)
            if not batch:
                break
            fetched, batch_errors = self._fetch(batch, max_chars=max_chars)
            pages.extend(fetched)
            errors.extend(batch_errors)
        return pages, errors

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
        by_url = {}
        for item in candidates:
            url = str(getattr(item, "url", "") or "").strip()
            if url and url not in by_url:
                by_url[url] = item
        ordered = _ordered_candidates(candidates, limit=max_pages + EXTRA_FETCH_ATTEMPTS)

        if not ordered:
            return {"query": normalized, "observations": [], "errors": [], "search_results": len(candidates)}

        pages, errors = self._render(ordered, wanted=max_pages, max_chars=max_chars_per_page)

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
