from __future__ import annotations

from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as package_version
import logging
import os
from pathlib import Path
import threading

from character_memory.app import AppBundle, build_app
from character_memory.api_contracts import (
    CharacterCapacityConfirmationRequired,
    CharacterCapacityExceeded,
    ChatImageRequest,
    ChatRequest,
    CreateCharacterRequest,
    MAX_ACTIVE_CHARACTERS,
    PersonaDraftRequest,
    SOFT_ACTIVE_CHARACTERS,
    SimulateRequest,
)
from character_memory.api_character_service import ApiCharacterService
from character_memory.api_resource_service import ApiResourceService
from character_memory.api_route_access import CoreApiRouteAccess
from character_memory.application.proactive_service import ProactiveService
from character_memory.config import (
    load_persona,
    load_settings,
    resolve_media_dir,
)
from character_memory.domain.models import EventType
from character_memory.images import load_image_catalog
from character_memory.core_character_web import attach_core_character_routes
from character_memory.core_direct_web import attach_core_direct_routes
from character_memory.core_resource_web import attach_core_resource_routes
from character_memory.logging_utils import configure_logging
from character_memory.media import MediaStorage
from character_memory.persona_builder import PersonaDraft
from character_memory.runtime_services import CharacterRuntimeAccess, build_runtime_services
from character_memory.storage.sqlite import SQLiteStore
from character_memory.web_assets import attach_static_assets
from character_memory.web_lifecycle import on_app_event


logger = logging.getLogger("character_memory.api")


def _package_version() -> str:
    """The installed package version, so the API cannot advertise a stale one.

    This string used to be hardcoded and had drifted years of nominal versions
    behind `pyproject.toml`, which makes /openapi.json and any client that reads
    it lie about what is running.
    """
    try:
        return package_version("character-memory")
    except PackageNotFoundError:
        return "0.0.0+unknown"
_PROACTIVE_POLL_SECONDS = 30.0

def create_api(config_path: str = "config.yaml", *, bundle: AppBundle | None = None):
    from fastapi import FastAPI, HTTPException

    configure_logging()
    own_bundle = bundle is None
    settings = bundle.settings if bundle is not None else load_settings(config_path)
    app_bundle = bundle
    read_store = bundle.store if bundle is not None else SQLiteStore(settings.db_path)
    media_storage = MediaStorage(
        resolve_media_dir(settings),
        max_bytes=int(getattr(settings, "media_max_bytes", 8 * 1024 * 1024)),
    )
    services = build_runtime_services(settings)
    runtime_error: str | None = None
    runtime_loading = False
    init_lock = threading.Lock()
    character_write_lock = threading.RLock()
    proactive_stop = threading.Event()
    proactive_thread: threading.Thread | None = None
    warmup_thread: threading.Thread | None = None

    def current_bundle() -> AppBundle | None:
        return app_bundle

    def get_bundle() -> AppBundle:
        nonlocal app_bundle, runtime_error, runtime_loading
        if app_bundle is not None:
            return app_bundle
        with init_lock:
            if app_bundle is not None:
                return app_bundle
            logger.info("api.runtime init start config=%s", config_path)
            runtime_loading = True
            try:
                app_bundle = build_app(config_path)
                runtime_error = None
                logger.info(
                    "api.runtime init ready model=%s vision_model=%s characters=%d total_ms=%.1f",
                    app_bundle.settings.chat_model,
                    app_bundle.settings.vision_model,
                    len(app_bundle.characters),
                    app_bundle.init_timings.get("total_ms", 0.0),
                )
                return app_bundle
            except Exception as exc:
                runtime_error = str(exc)
                logger.exception("api.runtime init failed error=%s", exc)
                raise
            finally:
                runtime_loading = False

    def require_bundle() -> AppBundle:
        try:
            return get_bundle()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Runtime 初始化失败：{exc}") from exc

    def register_runtime_character(profile: dict[str, str]) -> None:
        current = current_bundle()
        if current is None:
            return
        if not hasattr(current, "runtimes") or not hasattr(current, "embeddings") or not hasattr(current, "model"):
            raise RuntimeError("loaded runtime bundle does not support dynamic character registration")

        from character_memory.memory.recall import VectorRecall
        from character_memory.runtime.person_runtime import PersonRuntime

        character_id = profile["id"]
        persona = load_persona(profile["persona_path"])
        stickers = global_sticker_catalog()
        images = load_image_catalog(profile["persona_path"])
        recall = VectorRecall(current.store, current.embeddings, limit=getattr(settings, "recall_limit", 8))
        runtime = PersonRuntime(current.store, recall, current.embeddings, current.model, persona, stickers, images)
        current.runtimes[character_id] = runtime
        if isinstance(getattr(current.chat, "runtime", None), dict):
            current.chat.runtime[character_id] = runtime
        refresh_runtime_sticker_catalog(stickers)
        characters.refresh_cache()


    characters = ApiCharacterService(
        settings=settings,
        current_bundle=current_bundle,
        character_write_lock=character_write_lock,
        register_runtime_character=register_runtime_character,
        services=services,
    )
    character_profiles = characters.profiles
    public_profile = characters.public_profile
    refresh_character_cache = characters.refresh_cache
    _set_archived = characters.set_archived
    ensure_character = characters.ensure
    check_character_capacity = characters.check_capacity
    create_character_from_draft = characters.create_from_draft
    rollback_created_character = characters.rollback_created

    def refresh_voice_registry() -> dict[str, object]:
        """Best-effort GSV registry refresh after archive/restore.

        Archiving is a lifecycle change, not data deletion. The voice.yaml stays
        on disk, while a running GSV sidecar should immediately stop resolving
        that character id until it is restored.
        """
        try:
            from character_memory.tts_lab import GsvVoiceReloader

            reloader = GsvVoiceReloader(timeout_seconds=1.5)
            try:
                reloader.reload()
            finally:
                reloader.close()
            return {"ok": True, "reloaded": True}
        except Exception as exc:
            logger.warning("api.voice_registry reload skipped/failed error=%s", exc)
            return {"ok": False, "reloaded": False, "reason": str(exc) or exc.__class__.__name__}

    resources = ApiResourceService(
        settings=settings,
        read_store=read_store,
        media_storage=media_storage,
        current_bundle=current_bundle,
        require_bundle=require_bundle,
        character_profiles=character_profiles,
        ensure_character=ensure_character,
    )
    global_sticker_catalog = resources.global_sticker_catalog
    sticker_catalog_for = resources.sticker_catalog_for
    refresh_runtime_sticker_catalog = resources.refresh_runtime_sticker_catalog
    ai_sticker_tagger = resources.ai_sticker_tagger
    image_catalog_for = resources.image_catalog_for
    uploaded_media_payload = resources.uploaded_media_payload
    action_payload = resources.action_payload
    message_payload = resources.message_payload

    def history_payload(character_id: str, limit: int) -> dict:
        ensure_character(character_id)
        events = read_store.list_chat_events(character_id, limit=limit)
        trace_sources = read_store.list_runtime_trace_sources(character_id)
        messages = []
        for event in events:
            item = message_payload(event)
            item["has_trace"] = item["source_event_id"] in trace_sources
            messages.append(item)
        return {"character_id": character_id, "messages": messages}

    def character_summary(profile: dict[str, str]) -> dict:
        character_id = profile["id"]
        latest_chat_rows = read_store.list_chat_events(character_id, limit=1)
        latest_assistant_rows = read_store.list_events(
            character_id,
            limit=1,
            event_type=EventType.CHARACTER_MESSAGE.value,
        )
        latest_message = message_payload(latest_chat_rows[-1]) if latest_chat_rows else None
        latest_assistant_id = latest_assistant_rows[-1].id if latest_assistant_rows else None
        return {
            "id": character_id,
            "latest_message": latest_message,
            "latest_assistant_message_id": latest_assistant_id,
        }

    def dispatch_proactive_once() -> list[dict]:
        if not getattr(settings, "api_key", ""):
            return []
        now = datetime.now().astimezone()
        character_ids = [
            profile["id"]
            for profile in character_profiles()
            if "archived_at" not in profile
        ]
        gate = ProactiveService(read_store)
        if not character_ids or not gate.has_due(character_ids, now):
            return []
        current = get_bundle()
        service = ProactiveService(current.store, current.chat)
        outcomes = service.dispatch_due(character_ids, now)

        # Proactive intents bypass ReactionScheduler generation, but their
        # derived CHARACTER_MESSAGE facts still need the same SSE publication
        # and VOICE_MESSAGE materialization as an ordinary direct reaction.
        feature_state = getattr(app.state, "character_memory", None)
        scheduler = getattr(feature_state, "reaction_scheduler", None) if feature_state is not None else None
        if scheduler is not None:
            for outcome in outcomes:
                source_event_id = outcome.get("source_event_id")
                character_id = str(outcome.get("character_id") or "")
                if source_event_id is None or not character_id:
                    continue
                source_event = current.store.get_event(int(source_event_id))
                if source_event is None:
                    continue
                conversation_id = str(
                    source_event.metadata.get("conversation_id")
                    or f"{character_id}:proactive"
                )
                scheduler.publish_direct_responses(
                    current.store,
                    character_id,
                    conversation_id,
                    int(source_event_id),
                )

        if outcomes:
            logger.info("api.proactive dispatched=%d", len(outcomes))
        return outcomes

    def proactive_loop() -> None:
        logger.info("api.proactive loop_start poll_seconds=%.0f", _PROACTIVE_POLL_SECONDS)
        while not proactive_stop.is_set():
            try:
                dispatch_proactive_once()
            except Exception:
                logger.exception("api.proactive loop_error")
            proactive_stop.wait(_PROACTIVE_POLL_SECONDS)
        logger.info("api.proactive loop_stop")

    app = FastAPI(title="character-memory", version=_package_version())
    web_dir = Path(__file__).with_name("web")
    attach_static_assets(app, web_dir)

    def runtime_status() -> dict:
        return {
            "runtime_loaded": app_bundle is not None,
            "runtime_loading": runtime_loading,
            "runtime_error": runtime_error,
        }

    # One application runtime access point. Feature route modules (group chat,
    # future media tools) reuse this instead of creating their own model/store.
    app.state.character_memory = CharacterRuntimeAccess(
        settings=settings,
        get_bundle=get_bundle,
        require_bundle=require_bundle,
        store=lambda: app_bundle.store if app_bundle is not None else read_store,
        read_store=read_store,
        media_storage=media_storage,
        services=services,
        character_profiles=character_profiles,
        global_sticker_catalog=global_sticker_catalog,
        refresh_runtime_sticker_catalog=refresh_runtime_sticker_catalog,
    )
    # Encounter is a candidate layer, not a second character registry. It uses
    # these callbacks only when the user explicitly keeps a candidate.
    app.state.character_memory.create_character_from_draft = create_character_from_draft
    app.state.character_memory.rollback_created_character = rollback_created_character
    app.state.character_memory.check_character_capacity = check_character_capacity
    app.state.character_memory.soft_active_characters = SOFT_ACTIVE_CHARACTERS
    app.state.character_memory.max_active_characters = MAX_ACTIVE_CHARACTERS

    def warm_runtime() -> None:
        try:
            get_bundle()
        except Exception:
            # /health stays available and exposes runtime_error. Normal requests
            # return 503 through require_bundle instead of silently retrying a
            # remote model download (local embeddings are strict-offline).
            logger.exception("api.runtime warmup_failed")

    @on_app_event(app, "startup")
    def _startup():
        nonlocal proactive_thread, warmup_thread
        eager_warmup = os.getenv("CHARACTER_MEMORY_EAGER_WARMUP", "0").strip().lower() in {"1", "true", "yes", "on"}
        if own_bundle and eager_warmup and (warmup_thread is None or not warmup_thread.is_alive()) and app_bundle is None:
            warmup_thread = threading.Thread(
                target=warm_runtime,
                name="character-memory-runtime-warmup",
                daemon=True,
            )
            warmup_thread.start()
        if own_bundle and (proactive_thread is None or not proactive_thread.is_alive()):
            proactive_thread = threading.Thread(
                target=proactive_loop,
                name="character-memory-proactive",
                daemon=True,
            )
            proactive_thread.start()

    @on_app_event(app, "shutdown")
    def _shutdown():
        proactive_stop.set()
        if proactive_thread is not None and proactive_thread.is_alive():
            proactive_thread.join(timeout=1.0)
        # Feature modules may own background workers that still use the shared
        # SQLite/model bundle. Stop them before the core closes those resources.
        feature_state = getattr(app.state, "character_memory", None)
        for scheduler_name in ("space_scheduler", "group_autonomy_scheduler"):
            scheduler = getattr(feature_state, scheduler_name, None) if feature_state is not None else None
            if scheduler is not None:
                stop_scheduler = getattr(scheduler, "stop", None)
                if callable(stop_scheduler):
                    stop_scheduler()
        services.close()
        if own_bundle:
            if app_bundle is not None:
                app_bundle.close()
            read_store.close()

    route_access = CoreApiRouteAccess(
        settings=settings,
        web_dir=web_dir,
        read_store=read_store,
        media_storage=media_storage,
        current_bundle=current_bundle,
        require_bundle=require_bundle,
        runtime_status=runtime_status,
        proactive_poll_seconds=_PROACTIVE_POLL_SECONDS,
        character_profiles=character_profiles,
        public_profile=public_profile,
        set_archived=_set_archived,
        refresh_voice_registry=refresh_voice_registry,
        character_summary=character_summary,
        ensure_character=ensure_character,
        create_character_from_draft=create_character_from_draft,
        global_sticker_catalog=global_sticker_catalog,
        sticker_catalog_for=sticker_catalog_for,
        ai_sticker_tagger=ai_sticker_tagger,
        refresh_runtime_sticker_catalog=refresh_runtime_sticker_catalog,
        image_catalog_for=image_catalog_for,
        uploaded_media_payload=uploaded_media_payload,
        action_payload=action_payload,
        history_payload=history_payload,
    )
    attach_core_character_routes(app, route_access)
    attach_core_resource_routes(app, route_access)
    attach_core_direct_routes(app, route_access)

    return app
