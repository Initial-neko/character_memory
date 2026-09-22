from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable
from character_memory.avatars import AvatarSearchService, AvatarStore
from character_memory.browser_web import HeadlessBrowserWebFetcher
from character_memory.config import resolve_avatar_dir
from character_memory.search import BraveSearchProvider, SearchApiProvider, SearchProvider
from character_memory.visual_generation import ImageGenerationProvider, build_image_providers
from character_memory.world_observation import WorldObservationService


logger = logging.getLogger("character_memory.runtime_services")


def build_search_provider(settings) -> SearchProvider | None:
    """Build the shared external-search infrastructure once at composition time.

    Avatar search, autonomous Space image search and World Observation are
    separate product capabilities, but they consume the same provider contract.
    No HTTP route owns this provider.
    """

    provider_name = str(getattr(settings, "search_provider", "searchapi") or "searchapi").strip().lower()
    provider_kwargs = {
        "country": getattr(settings, "search_country", "jp"),
        "language": getattr(settings, "search_language", "zh-cn"),
        "safe_search": getattr(settings, "search_safe_search", "strict"),
    }
    api_key = getattr(settings, "search_api_key", "")
    if provider_name in {"searchapi", "searchapi.io", "search_api"}:
        return SearchApiProvider(api_key, **provider_kwargs)
    if provider_name == "brave":
        return BraveSearchProvider(api_key, **provider_kwargs)
    return None


@dataclass
class RuntimeServices:
    """Infrastructure/services shared by Character Runtime feature adapters.

    This is the composition root for capabilities that used to be constructed
    inside route attach functions. Route order must not decide whether Search,
    Avatar, ImageGen or World Observation exists.
    """

    search_provider: SearchProvider | None
    avatar_store: AvatarStore
    avatar_search: AvatarSearchService
    image_generation_providers: dict[str, ImageGenerationProvider]
    world_fetcher: HeadlessBrowserWebFetcher
    world_observer: WorldObservationService

    def close(self) -> None:
        """Close resources once, after feature workers have stopped."""

        for provider in self.image_generation_providers.values():
            try:
                provider.close()
            except Exception:
                logger.exception(
                    "runtime_services image_provider close_failed provider=%s",
                    getattr(provider, "name", "unknown"),
                )
        if self.search_provider is not None:
            try:
                self.search_provider.close()
            except Exception:
                logger.exception("runtime_services search_provider close_failed")
        try:
            self.avatar_store.close()
        except Exception:
            logger.exception("runtime_services avatar_store close_failed")



@dataclass
class CharacterRuntimeAccess:
    """Typed core access object exposed through app.state.character_memory.

    Feature modules may still attach their process-local runtime handles
    (scheduler, stream hub, etc.) while the stable infrastructure contract stays
    explicit and type-readable here.
    """

    settings: Any
    get_bundle: Callable[[], Any]
    require_bundle: Callable[[], Any]
    store: Callable[[], Any]
    read_store: Any
    media_storage: Any
    services: RuntimeServices
    character_profiles: Callable[[], list[dict[str, str]]]
    global_sticker_catalog: Callable[[], Any]
    refresh_runtime_sticker_catalog: Callable[[Any], None]

    @property
    def avatar_store(self):
        return self.services.avatar_store

    @property
    def avatar_search(self):
        return self.services.avatar_search

    @property
    def image_generation_providers(self):
        return self.services.image_generation_providers

    @property
    def world_fetcher(self):
        return self.services.world_fetcher

    @property
    def world_observer(self):
        return self.services.world_observer

def build_runtime_services(settings) -> RuntimeServices:
    search_provider = build_search_provider(settings)
    avatar_store = AvatarStore(
        resolve_avatar_dir(settings),
        max_bytes=int(getattr(settings, "avatar_max_bytes", 8 * 1024 * 1024)),
    )
    avatar_search = AvatarSearchService(search_provider, avatar_store)
    image_generation_providers = build_image_providers(settings)
    world_fetcher = HeadlessBrowserWebFetcher(
        timeout_seconds=float(getattr(settings, "web_browser_timeout_seconds", 20.0)),
        render_wait_ms=int(getattr(settings, "web_browser_render_wait_ms", 700)),
        channel=str(getattr(settings, "web_browser_channel", "auto") or "auto"),
    )
    world_observer = WorldObservationService(search_provider, world_fetcher)
    return RuntimeServices(
        search_provider=search_provider,
        avatar_store=avatar_store,
        avatar_search=avatar_search,
        image_generation_providers=image_generation_providers,
        world_fetcher=world_fetcher,
        world_observer=world_observer,
    )
