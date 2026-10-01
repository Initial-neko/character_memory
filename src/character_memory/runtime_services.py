from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable
from character_memory.avatars import AvatarSearchService, AvatarStore
from character_memory.browser_web import HeadlessBrowserWebFetcher
from character_memory.config import Settings, resolve_avatar_dir, runtime_setting
from character_memory.search import BraveSearchProvider, SearchApiProvider, SearchProvider
from character_memory.visual_generation import ImageGenerationProvider, build_image_providers
from character_memory.world_observation import WorldObservationService


logger = logging.getLogger("character_memory.runtime_services")


def build_search_provider(settings: Settings) -> SearchProvider | None:
    """Build the shared external-search infrastructure once at composition time.

    Avatar search, autonomous Space image search and World Observation are
    separate product capabilities, but they consume the same provider contract.
    No HTTP route owns this provider.
    """

    provider_name = str(runtime_setting(settings, "search_provider", "searchapi") or "searchapi").strip().lower()
    provider_kwargs = {
        "country": runtime_setting(settings, "search_country", "jp"),
        "language": runtime_setting(settings, "search_language", "zh-cn"),
        "safe_search": runtime_setting(settings, "search_safe_search", "strict"),
    }
    api_key = runtime_setting(settings, "search_api_key", "")
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

    The core contract is declared here. Feature modules additionally attach
    process-local runtime handles after construction, and those are declared
    below so the whole surface of app.state.character_memory is readable from
    this file alone. Each feature field names the module that attaches it and
    stays None until that module's attach_* function has run; consumers keep
    probing with getattr(access, name, None).
    """

    settings: Settings
    get_bundle: Callable[[], Any]
    require_bundle: Callable[[], Any]
    store: Callable[[], Any]
    read_store: Any
    media_storage: Any
    services: RuntimeServices
    character_profiles: Callable[[], list[dict[str, str]]]
    global_sticker_catalog: Callable[[], Any]
    refresh_runtime_sticker_catalog: Callable[[Any], None]

    # Encounter is a candidate layer, not a second character registry. It uses
    # these callbacks only when the user explicitly keeps a candidate.
    create_character_from_draft: Callable[..., Any]
    rollback_created_character: Callable[..., Any]
    check_character_capacity: Callable[..., Any]
    soft_active_characters: int
    max_active_characters: int

    # Attached by feature route modules during Server composition.
    stream_hub: Any | None = None  # async_web
    reaction_scheduler: Any | None = None  # async_web
    encounter_repository: Any | None = None  # encounter_web
    encounter_service: Any | None = None  # encounter_web
    encounter_scheduler: Any | None = None  # encounter_web
    ensemble_repository: Any | None = None  # ensemble_web
    ensemble_service: Any | None = None  # ensemble_web
    group_autonomy_repository: Any | None = None  # group_autonomy_web
    group_autonomy: Any | None = None  # group_autonomy_web
    group_autonomy_scheduler: Any | None = None  # group_autonomy_web
    space_repository: Any | None = None  # space_web
    space_media_repository: Any | None = None  # space_web
    space_autonomy: Any | None = None  # space_web
    space_scheduler: Any | None = None  # space_web
    visual_runtime: Any | None = None  # visual_runtime
    world_activity_scheduler: Any | None = None  # world_web
    wake_service: Any | None = None  # wake_web

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

def build_runtime_services(settings: Settings) -> RuntimeServices:
    search_provider = build_search_provider(settings)
    avatar_store = AvatarStore(
        resolve_avatar_dir(settings),
        max_bytes=int(runtime_setting(settings, "avatar_max_bytes", 8 * 1024 * 1024)),
    )
    avatar_search = AvatarSearchService(search_provider, avatar_store)
    image_generation_providers = build_image_providers(settings)
    world_fetcher = HeadlessBrowserWebFetcher(
        timeout_seconds=float(runtime_setting(settings, "web_browser_timeout_seconds", 20.0)),
        render_wait_ms=int(runtime_setting(settings, "web_browser_render_wait_ms", 700)),
        channel=str(runtime_setting(settings, "web_browser_channel", "auto") or "auto"),
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
