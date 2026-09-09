from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import threading
import time

from pydantic import BaseModel, Field

from character_memory.app import AppBundle, build_app
from character_memory.config import load_persona, load_settings
from character_memory.domain.models import EventType
from character_memory.logging_utils import configure_logging
from character_memory.storage.sqlite import SQLiteStore


logger = logging.getLogger("character_memory.api")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    character_id: str = "rin"
    conversation_id: str = "default"
    at: datetime | None = None


class SimulateRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=365)
    character_id: str = "rin"


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
                logger.info("api.runtime lazy_init ready model=%s total_ms=%.1f", app_bundle.settings.chat_model, app_bundle.init_timings.get("total_ms", 0.0))
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

    def history_payload(character_id: str, limit: int) -> dict:
        events = read_store.list_chat_events(character_id, limit=limit)
        trace_sources = read_store.list_runtime_trace_sources(character_id)
        messages = []
        for event in events:
            if event.event_type == EventType.USER_MESSAGE:
                role = "user"
                source_event_id = event.id
            else:
                role = "assistant"
                source_event_id = event.metadata.get("source_event_id")
            messages.append({"id": event.id, "role": role, "content": event.content, "event_time": event.event_time.isoformat(), "action": event.metadata.get("action"), "source_event_id": source_event_id, "has_trace": source_event_id in trace_sources})
        return {"character_id": character_id, "messages": messages}

    app = FastAPI(title="character-memory", version="0.4.3")
    web_dir = Path(__file__).with_name("web")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.on_event("shutdown")
    def _shutdown():
        if own_bundle:
            if app_bundle is not None:
                app_bundle.close()
            read_store.close()

    @app.get("/")
    def web_index():
        return FileResponse(web_dir / "index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "web": "ready", "runtime_loaded": app_bundle is not None, "runtime_error": runtime_error, "model": settings.chat_model, "embedding_provider": settings.embedding_provider, "db_path": settings.db_path}

    @app.get("/v1/chat/history")
    def history(character_id: str = "rin", limit: int = 160):
        return history_payload(character_id, max(1, min(limit, 500)))

    @app.post("/v1/chat")
    def chat(req: ChatRequest):
        api_started = time.perf_counter()
        was_unloaded = app_bundle is None
        logger.info("api.chat start character=%s conversation=%s chars=%d runtime_loaded=%s", req.character_id, req.conversation_id, len(req.message), not was_unloaded)

        current = require_bundle()
        init_ms = current.init_timings.get("total_ms", 0.0) if was_unloaded else 0.0

        service_started = time.perf_counter()
        try:
            out = current.chat.send(req.message, character_id=req.character_id, conversation_id=req.conversation_id, at=req.at)
        except Exception:
            logger.exception("api.chat failed character=%s conversation=%s elapsed_ms=%.1f", req.character_id, req.conversation_id, _ms(api_started))
            raise
        service_ms = _ms(service_started)
        api_total_ms = _ms(api_started)
        timings = dict(out.timings)
        timings["runtime_init_ms"] = round(init_ms, 1)
        timings["chat_service_ms"] = service_ms
        timings["api_total_ms"] = api_total_ms
        logger.info("api.chat timings event_id=%s %s", out.event.id, " ".join(f"{key}={value:.1f}ms" for key, value in timings.items()))

        return {
            "event_id": out.event.id,
            "event_time": out.event.event_time.isoformat(),
            "action": out.reaction.action.model_dump(mode="json"),
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
        memories = read_store.list_memories(character_id, include_inactive=True, limit=80, include_embedding=False)
        intents = [dict(row) for row in read_store.list_intents(character_id, limit=80)]
        return {
            "character_id": character_id,
            "now": datetime.now().astimezone().isoformat(),
            "provider": {"chat_model": settings.chat_model, "base_url": settings.base_url, "embedding_provider": settings.embedding_provider, "embedding_model": settings.embedding_model, "db_path": settings.db_path},
            "runtime_loaded": app_bundle is not None,
            "runtime_error": runtime_error,
            "runtime_init_timings": app_bundle.init_timings if app_bundle is not None else {},
            "persona": load_persona(settings.persona_path),
            "mental_state": read_store.get_mental_state(character_id),
            "memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in reversed(memories)],
            "intents": intents,
        }

    @app.post("/v1/simulate")
    def simulate(req: SimulateRequest):
        current = require_bundle()
        return {"days": current.days.simulate(req.character_id, req.days)}

    @app.get("/v1/state/{character_id}")
    def state(character_id: str):
        return {"world_time": read_store.get_world_time(character_id), "mental_state": read_store.get_mental_state(character_id), "recent_events": [e.model_dump(mode="json") for e in read_store.list_events(character_id, 30)], "memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in read_store.list_memories(character_id, limit=30, include_embedding=False)], "intents": [dict(r) for r in read_store.list_intents(character_id)]}

    return app
