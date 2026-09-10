from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import threading
import time

from pydantic import BaseModel, Field, model_validator

from character_memory.app import AppBundle, build_app, build_model
from character_memory.application.proactive_service import ProactiveService
from character_memory.config import discover_character_profiles, load_persona, load_settings, resolve_persona_path
from character_memory.domain.models import EventType
from character_memory.logging_utils import configure_logging
from character_memory.persona_builder import PersonaBuilder, PersonaDraft, normalize_character_id, save_persona
from character_memory.stickers import load_sticker_catalog
from character_memory.storage.sqlite import SQLiteStore


logger = logging.getLogger("character_memory.api")
_PROACTIVE_POLL_SECONDS = 30.0


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=12000)
    sticker_id: str | None = Field(default=None, max_length=64)
    character_id: str = "rin"
    conversation_id: str = "default"
    at: datetime | None = None

    @model_validator(mode="after")
    def require_content(self):
        if not self.message.strip() and not (self.sticker_id or "").strip():
            raise ValueError("message or sticker_id is required")
        return self


class SimulateRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=365)
    character_id: str = "rin"


class PersonaDraftRequest(BaseModel):
    description: str = Field(min_length=3, max_length=4000)
    name: str = Field(default="", max_length=48)
    age: int | None = Field(default=None, ge=1, le=120)
    tags: list[str] = Field(default_factory=list, max_length=8)


class CreateCharacterRequest(BaseModel):
    draft: PersonaDraft
    character_id: str = Field(default="", max_length=32)


def create_api(config_path: str = "config.yaml", *, bundle: AppBundle | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    configure_logging()
    own_bundle = bundle is None
    settings = bundle.settings if bundle is not None else load_settings(config_path)
    app_bundle = bundle
    read_store = bundle.store if bundle is not None else SQLiteStore(settings.db_path)
    runtime_error: str | None = None
    init_lock = threading.Lock()
    character_write_lock = threading.RLock()
    proactive_stop = threading.Event()
    proactive_thread: threading.Thread | None = None

    def get_bundle() -> AppBundle:
        nonlocal app_bundle, runtime_error
        if app_bundle is not None:
            return app_bundle
        with init_lock:
            if app_bundle is not None:
                return app_bundle
            logger.info("api.runtime lazy_init start config=%s", config_path)
            try:
                app_bundle = build_app(config_path)
                runtime_error = None
                logger.info(
                    "api.runtime lazy_init ready model=%s characters=%d total_ms=%.1f",
                    app_bundle.settings.chat_model,
                    len(app_bundle.characters),
                    app_bundle.init_timings.get("total_ms", 0.0),
                )
                return app_bundle
            except Exception as exc:
                runtime_error = str(exc)
                logger.exception("api.runtime lazy_init failed error=%s", exc)
                raise

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

    def ensure_character(character_id: str) -> dict[str, str]:
        for profile in character_profiles():
            if profile["id"] == character_id:
                return profile
        known = ", ".join(profile["id"] for profile in character_profiles())
        raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}. Known: {known}")

    def sticker_catalog_for(character_id: str):
        profile = ensure_character(character_id)
        if app_bundle is not None and hasattr(app_bundle, "runtimes"):
            runtime = app_bundle.runtimes.get(character_id)
            if runtime is not None and getattr(runtime, "sticker_catalog", None) is not None:
                return runtime.sticker_catalog
        return load_sticker_catalog(profile["persona_path"])

    def sticker_payload(character_id: str, sticker_id: str | None) -> dict | None:
        if not sticker_id:
            return None
        catalog = sticker_catalog_for(character_id)
        sticker = catalog.get(sticker_id)
        if sticker is None or catalog.asset_path(sticker_id) is None:
            return None
        return {
            **sticker.model_dump(mode="json"),
            "url": f"/v1/stickers/{character_id}/{sticker.id}/asset",
        }

    def action_payload(character_id: str, action) -> dict:
        item = action.model_dump(mode="json")
        sticker = sticker_payload(character_id, item.get("sticker_id"))
        if sticker is not None:
            item["sticker"] = sticker
        return item

    def message_payload(event) -> dict:
        if event.event_type == EventType.USER_MESSAGE:
            role = "user"
            source_event_id = event.id
            source_event_type = EventType.USER_MESSAGE.value
            content = event.metadata.get("display_text", event.content)
        else:
            role = "assistant"
            source_event_id = event.metadata.get("source_event_id")
            source_event_type = event.metadata.get("source_event_type")
            content = event.content
        sticker_id = event.metadata.get("sticker_id")
        sticker = sticker_payload(event.character_id, sticker_id)
        preview = content
        if sticker is not None and not str(content or "").strip():
            preview = f"[表情包] {sticker['label']}"
        elif sticker is not None and event.metadata.get("action") == "STICKER":
            preview = f"[表情包] {sticker['label']}"
        return {
            "id": event.id,
            "role": role,
            "content": content,
            "preview": preview,
            "event_time": event.event_time.isoformat(),
            "action": event.metadata.get("action"),
            "action_index": event.metadata.get("action_index"),
            "sticker_id": sticker_id,
            "sticker": sticker,
            "source_event_type": source_event_type,
            "source_event_id": source_event_id,
            "proactive": source_event_type == EventType.PROACTIVE_INTENT.value,
        }

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
        stickers = load_sticker_catalog(profile["persona_path"])
        recall = VectorRecall(app_bundle.store, app_bundle.embeddings, limit=getattr(settings, "recall_limit", 8))
        runtime = PersonRuntime(app_bundle.store, recall, app_bundle.embeddings, app_bundle.model, persona, stickers)
        app_bundle.runtimes[character_id] = runtime
        if isinstance(getattr(app_bundle.chat, "runtime", None), dict):
            app_bundle.chat.runtime[character_id] = runtime
        if hasattr(app_bundle, "characters"):
            app_bundle.characters[:] = discover_character_profiles(settings)

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

    app = FastAPI(title="character-memory", version="0.8.0")
    web_dir = Path(__file__).with_name("web")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.on_event("startup")
    def _startup():
        nonlocal proactive_thread
        if own_bundle and (proactive_thread is None or not proactive_thread.is_alive()):
            proactive_thread = threading.Thread(
                target=proactive_loop,
                name="character-memory-proactive",
                daemon=True,
            )
            proactive_thread.start()

    @app.on_event("shutdown")
    def _shutdown():
        proactive_stop.set()
        if proactive_thread is not None and proactive_thread.is_alive():
            proactive_thread.join(timeout=1.0)
        if own_bundle:
            if app_bundle is not None:
                app_bundle.close()
            read_store.close()

    @app.get("/")
    def web_index():
        return FileResponse(web_dir / "index.html")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "web": "ready",
            "runtime_loaded": app_bundle is not None,
            "runtime_error": runtime_error,
            "model": settings.chat_model,
            "embedding_provider": settings.embedding_provider,
            "db_path": settings.db_path,
            "characters": len(character_profiles()),
            "proactive_poll_seconds": _PROACTIVE_POLL_SECONDS,
        }

    @app.get("/v1/characters")
    def characters():
        return {"characters": [public_profile(profile) for profile in character_profiles()]}

    @app.get("/v1/characters/summaries")
    def character_summaries():
        return {"characters": [character_summary(profile) for profile in character_profiles()]}

    @app.get("/v1/stickers")
    def stickers(character_id: str = "rin"):
        catalog = sticker_catalog_for(character_id)
        return {
            "character_id": character_id,
            "source": catalog.source,
            "stickers": catalog.public_items(character_id),
        }

    @app.get("/v1/stickers/{character_id}/{sticker_id}/asset")
    def sticker_asset(character_id: str, sticker_id: str):
        catalog = sticker_catalog_for(character_id)
        path = catalog.asset_path(sticker_id)
        if path is None:
            raise HTTPException(status_code=404, detail="sticker not found")
        return FileResponse(path)

    @app.post("/v1/characters/draft")
    def generate_character_draft(req: PersonaDraftRequest):
        started = time.perf_counter()
        temporary_model = None
        try:
            if app_bundle is not None and hasattr(app_bundle, "model"):
                model = app_bundle.model
            else:
                temporary_model = build_model(settings)
                model = temporary_model
            draft = PersonaBuilder(model).generate(req.description, name=req.name, age=req.age, tags=req.tags)
            logger.info("api.persona_draft done name=%s duration_ms=%.1f", draft.name, _ms(started))
            return {"draft": draft.model_dump(mode="json")}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("api.persona_draft failed duration_ms=%.1f error=%s", _ms(started), exc)
            raise HTTPException(status_code=502, detail=f"人物草稿生成失败：{exc}") from exc
        finally:
            if temporary_model is not None:
                temporary_model.close()

    @app.post("/v1/characters")
    def create_character(req: CreateCharacterRequest):
        with character_write_lock:
            character_id = normalize_character_id(req.draft.name, req.character_id)
            try:
                path = save_persona(settings.persona_path, req.draft, character_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except FileExistsError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            try:
                profiles = discover_character_profiles(settings)
                profile = next(profile for profile in profiles if profile["id"] == character_id)
                register_runtime_character(profile)
            except Exception as exc:
                try:
                    path.unlink(missing_ok=True)
                    path.parent.rmdir()
                except OSError:
                    logger.exception("api.character rollback_file failed character=%s", character_id)
                logger.exception("api.character create failed character=%s error=%s", character_id, exc)
                raise HTTPException(status_code=500, detail=f"人物创建失败：{exc}") from exc

            logger.info("api.character created character=%s path=%s runtime_loaded=%s", character_id, path, app_bundle is not None)
            return {"character": public_profile(profile), "description": req.draft.description}

    @app.get("/v1/chat/history")
    def history(character_id: str = "rin", limit: int = 160):
        return history_payload(character_id, max(1, min(limit, 500)))

    @app.post("/v1/chat")
    def chat(req: ChatRequest):
        ensure_character(req.character_id)
        selected_sticker = None
        if req.sticker_id:
            catalog = sticker_catalog_for(req.character_id)
            sticker = catalog.get(req.sticker_id)
            if sticker is None or catalog.asset_path(req.sticker_id) is None:
                raise HTTPException(status_code=400, detail=f"Unknown sticker: {req.sticker_id}")
            selected_sticker = sticker.model_dump(mode="json")

        api_started = time.perf_counter()
        was_unloaded = app_bundle is None
        logger.info(
            "api.chat start character=%s conversation=%s chars=%d sticker=%s runtime_loaded=%s",
            req.character_id,
            req.conversation_id,
            len(req.message),
            req.sticker_id or "-",
            not was_unloaded,
        )

        current = require_bundle()
        init_ms = current.init_timings.get("total_ms", 0.0) if was_unloaded else 0.0

        service_started = time.perf_counter()
        try:
            out = current.chat.send(
                req.message,
                character_id=req.character_id,
                conversation_id=req.conversation_id,
                at=req.at,
                sticker=selected_sticker,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception:
            logger.exception("api.chat failed character=%s conversation=%s elapsed_ms=%.1f", req.character_id, req.conversation_id, _ms(api_started))
            raise
        service_ms = _ms(service_started)
        api_total_ms = _ms(api_started)
        timings = dict(out.timings)
        timings["runtime_init_ms"] = round(init_ms, 1)
        timings["chat_service_ms"] = service_ms
        timings["api_total_ms"] = api_total_ms
        logger.info("api.chat timings event_id=%s character=%s %s", out.event.id, req.character_id, " ".join(f"{key}={value:.1f}ms" for key, value in timings.items()))

        return {
            "event_id": out.event.id,
            "event_time": out.event.event_time.isoformat(),
            "character_id": req.character_id,
            "action": action_payload(req.character_id, out.reaction.action) if out.reaction.action is not None else None,
            "actions": [action_payload(req.character_id, action) for action in out.reaction.actions],
            "perception": out.reaction.perception,
            "reaction": out.reaction.reaction,
            "mental_state": out.reaction.mental_state_update,
            "recalled_memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in out.recalled_memories],
            "created_memory_ids": out.created_memory_ids,
            "created_intent_ids": out.created_intent_ids,
            "timings": timings,
        }

    @app.get("/v1/traces/{source_event_id}")
    def trace(source_event_id: int):
        value = read_store.get_runtime_trace(source_event_id)
        if value is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return value

    @app.get("/v1/runtime/{character_id}")
    def runtime_state(character_id: str):
        profile = ensure_character(character_id)
        memories = read_store.list_memories(character_id, include_inactive=True, limit=80, include_embedding=False)
        intents = [dict(row) for row in read_store.list_intents(character_id, limit=80)]
        catalog = sticker_catalog_for(character_id)
        if app_bundle is not None and hasattr(app_bundle, "runtimes") and character_id in app_bundle.runtimes:
            persona = app_bundle.runtimes[character_id].persona
        else:
            persona_path = profile.get("persona_path") or resolve_persona_path(settings, character_id)
            persona = load_persona(persona_path)
        return {
            "character_id": character_id,
            "profile": public_profile(profile),
            "now": datetime.now().astimezone().isoformat(),
            "provider": {"chat_model": settings.chat_model, "base_url": settings.base_url, "embedding_provider": settings.embedding_provider, "embedding_model": settings.embedding_model, "db_path": settings.db_path},
            "runtime_loaded": app_bundle is not None,
            "runtime_error": runtime_error,
            "runtime_init_timings": app_bundle.init_timings if app_bundle is not None else {},
            "persona": persona,
            "mental_state": read_store.get_mental_state(character_id),
            "memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in reversed(memories)],
            "intents": intents,
            "stickers": catalog.public_items(character_id),
            "sticker_source": catalog.source,
        }

    @app.post("/v1/simulate")
    def simulate(req: SimulateRequest):
        current = require_bundle()
        return {"days": current.days.simulate(req.character_id, req.days)}

    @app.get("/v1/state/{character_id}")
    def state(character_id: str):
        ensure_character(character_id)
        return {"world_time": read_store.get_world_time(character_id), "mental_state": read_store.get_mental_state(character_id), "recent_events": [e.model_dump(mode="json") for e in read_store.list_events(character_id, 30)], "memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in read_store.list_memories(character_id, limit=30, include_embedding=False)], "intents": [dict(r) for r in read_store.list_intents(character_id)]}

    return app
