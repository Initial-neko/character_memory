from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import shutil
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
from character_memory.api_resource_service import ApiResourceService
from character_memory.api_route_access import CoreApiRouteAccess
from character_memory.application.proactive_service import ProactiveService
from character_memory.config import (
    discover_character_profiles,
    load_persona,
    load_settings,
    resolve_media_dir,
    resolve_persona_path,
    set_character_archived,
    split_archived,
)
from character_memory.domain.models import EventType
from character_memory.images import load_image_catalog
from character_memory.core_character_web import attach_core_character_routes
from character_memory.core_direct_web import attach_core_direct_routes
from character_memory.core_resource_web import attach_core_resource_routes
from character_memory.logging_utils import configure_logging
from character_memory.media import MediaStorage
from character_memory.message_projection import project_direct_message
from character_memory.persona_builder import PersonaDraft, normalize_character_id, save_persona
from character_memory.runtime_services import CharacterRuntimeAccess, build_runtime_services
from character_memory.storage.sqlite import SQLiteStore
from character_memory.web_assets import attach_static_assets
from character_memory.web_lifecycle import on_app_event


logger = logging.getLogger("character_memory.api")
_PROACTIVE_POLL_SECONDS = 30.0
_STICKER_VISION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


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

    def character_profiles() -> list[dict[str, str]]:
        if app_bundle is not None and hasattr(app_bundle, "characters"):
            return list(app_bundle.characters)
        try:
            return discover_character_profiles(settings)
        except (AttributeError, OSError):
            return [{"id": "rin", "name": "Rin", "identity": "", "tagline": "", "persona_path": getattr(settings, "persona_path", "personas/rin/persona.yaml")}]

    def public_profile(profile: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in profile.items() if key != "persona_path"}

    def refresh_character_cache() -> None:
        """Re-read the persona tree into the cached bundle list.

        The listing filter reads ``archived_at`` off a profile, so a cache built
        before the marker was written would keep serving the archived character
        as active. The cache is never filtered itself -- ``ensure_character`` and
        the group payloads resolve archived characters on purpose -- so only this
        refresh matters.
        """

        if app_bundle is not None and hasattr(app_bundle, "characters"):
            app_bundle.characters[:] = discover_character_profiles(settings)

    def _set_archived(
        character_id: str,
        *,
        archived: bool,
        confirm_over_soft_limit: bool = False,
    ) -> dict:
        """Archive or restore one character. Idempotent, like the group routes."""

        with character_write_lock:
            try:
                resolve_persona_path(settings, character_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            profiles = character_profiles()
            target = next((item for item in profiles if item["id"] == character_id), None)
            active_profiles = split_archived(profiles, False)
            if archived and target is not None and "archived_at" not in target:
                if len(active_profiles) <= 1:
                    raise HTTPException(status_code=409, detail="至少保留一个未归档人物")
            if not archived and target is not None and "archived_at" in target:
                active_count = len(active_profiles)
                if active_count >= MAX_ACTIVE_CHARACTERS:
                    raise HTTPException(
                        status_code=409,
                        detail=CharacterCapacityExceeded(active_count, 1).detail(),
                    )
                if active_count >= SOFT_ACTIVE_CHARACTERS and not confirm_over_soft_limit:
                    raise HTTPException(
                        status_code=409,
                        detail=CharacterCapacityConfirmationRequired(active_count, 1).detail(),
                    )

            try:
                stamp = set_character_archived(settings, character_id, archived)
            except OSError as exc:
                logger.exception("api.character archive failed character=%s error=%s", character_id, exc)
                raise HTTPException(status_code=500, detail=f"归档失败：{exc}") from exc

            refresh_character_cache()
            logger.info("api.character archived=%s character=%s", archived, character_id)

            profile = next(
                (item for item in character_profiles() if item["id"] == character_id), None
            )
            return {
                "ok": True,
                "archived": archived,
                "archived_at": stamp,
                "character": public_profile(profile) if profile else {"id": character_id},
            }

    def ensure_character(character_id: str) -> dict[str, str]:
        for profile in character_profiles():
            if profile["id"] == character_id:
                return profile
        known = ", ".join(profile["id"] for profile in character_profiles())
        raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}. Known: {known}")

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

    def register_runtime_character(profile: dict[str, str]) -> None:
        if app_bundle is None:
            return
        if not hasattr(app_bundle, "runtimes") or not hasattr(app_bundle, "embeddings") or not hasattr(app_bundle, "model"):
            raise RuntimeError("loaded runtime bundle does not support dynamic character registration")

        from character_memory.memory.recall import VectorRecall
        from character_memory.runtime.person_runtime import PersonRuntime

        character_id = profile["id"]
        persona = load_persona(profile["persona_path"])
        stickers = global_sticker_catalog()
        images = load_image_catalog(profile["persona_path"])
        recall = VectorRecall(app_bundle.store, app_bundle.embeddings, limit=getattr(settings, "recall_limit", 8))
        runtime = PersonRuntime(app_bundle.store, recall, app_bundle.embeddings, app_bundle.model, persona, stickers, images)
        app_bundle.runtimes[character_id] = runtime
        if isinstance(getattr(app_bundle.chat, "runtime", None), dict):
            app_bundle.chat.runtime[character_id] = runtime
        refresh_runtime_sticker_catalog(stickers)
        refresh_character_cache()

    def check_character_capacity(
        add_count: int = 1,
        *,
        confirm_over_soft_limit: bool = False,
    ) -> dict:
        count = max(0, int(add_count))
        active_count = len(split_archived(character_profiles(), False))
        result_count = active_count + count
        if result_count > MAX_ACTIVE_CHARACTERS:
            raise CharacterCapacityExceeded(active_count, count)
        if count and result_count > SOFT_ACTIVE_CHARACTERS and not confirm_over_soft_limit:
            raise CharacterCapacityConfirmationRequired(active_count, count)
        return {
            "active_count": active_count,
            "add_count": count,
            "result_count": result_count,
            "soft_limit": SOFT_ACTIVE_CHARACTERS,
            "hard_limit": MAX_ACTIVE_CHARACTERS,
            "warning": result_count > SOFT_ACTIVE_CHARACTERS,
        }

    def create_character_from_draft(
        draft: PersonaDraft,
        requested_id: str = "",
        *,
        confirm_over_soft_limit: bool = False,
        skip_capacity_check: bool = False,
    ) -> dict:
        with character_write_lock:
            if not skip_capacity_check:
                check_character_capacity(
                    1,
                    confirm_over_soft_limit=confirm_over_soft_limit,
                )
            character_id = normalize_character_id(draft.name, requested_id)
            path = save_persona(settings.persona_path, draft, character_id)
            try:
                profiles = discover_character_profiles(settings)
                profile = next(profile for profile in profiles if profile["id"] == character_id)
                register_runtime_character(profile)
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                    path.parent.rmdir()
                except OSError:
                    logger.exception("api.character rollback_file failed character=%s", character_id)
                raise
            logger.info(
                "api.character created character=%s path=%s runtime_loaded=%s",
                character_id,
                path,
                app_bundle is not None,
            )
            return profile

    def rollback_created_character(character_id: str) -> None:
        """Best-effort rollback for a character created inside a batch build."""
        with character_write_lock:
            profile = next(
                (item for item in discover_character_profiles(settings) if item["id"] == character_id),
                None,
            )
            if profile is None:
                return
            if app_bundle is not None:
                if hasattr(app_bundle, "runtimes"):
                    app_bundle.runtimes.pop(character_id, None)
                runtime_map = getattr(getattr(app_bundle, "chat", None), "runtime", None)
                if isinstance(runtime_map, dict):
                    runtime_map.pop(character_id, None)
            persona_path = Path(profile["persona_path"])
            try:
                shutil.rmtree(persona_path.parent)
            except FileNotFoundError:
                pass
            refresh_character_cache()

    def dispatch_proactive_once() -> list[dict]:
        if not getattr(settings, "api_key", ""):
            return []
        now = datetime.now().astimezone()
        character_ids = [profile["id"] for profile in character_profiles()]
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

    app = FastAPI(title="character-memory", version="0.12.0")
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
        character_profiles=character_profiles,
        public_profile=public_profile,
        set_archived=_set_archived,
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
