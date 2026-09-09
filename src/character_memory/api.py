from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import threading

from pydantic import BaseModel, Field

from character_memory.app import AppBundle, build_app
from character_memory.config import load_settings
from character_memory.logging_utils import configure_logging


logger = logging.getLogger("character_memory.api")


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
                logger.info("api.runtime lazy_init ready model=%s", app_bundle.settings.chat_model)
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

    app = FastAPI(title="character-memory", version="0.4.2")
    web_dir = Path(__file__).with_name("web")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.on_event("shutdown")
    def _shutdown():
        if own_bundle and app_bundle is not None:
            app_bundle.close()

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
        }

    @app.get("/v1/chat/history")
    def history(character_id: str = "rin", limit: int = 160):
        current = require_bundle()
        limit = max(1, min(limit, 500))
        return current.chat.history(character_id, limit)

    @app.post("/v1/chat")
    def chat(req: ChatRequest):
        current = require_bundle()
        logger.info("api.chat character=%s conversation=%s chars=%d", req.character_id, req.conversation_id, len(req.message))
        out = current.chat.send(req.message, character_id=req.character_id, conversation_id=req.conversation_id, at=req.at)
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
        }

    @app.get("/v1/traces/{source_event_id}")
    def trace(source_event_id: int):
        current = require_bundle()
        value = current.store.get_runtime_trace(source_event_id)
        if value is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return value

    @app.get("/v1/runtime/{character_id}")
    def runtime_state(character_id: str):
        current = require_bundle()
        memories = current.store.list_memories(character_id, include_inactive=True, limit=80, include_embedding=False)
        intents = [dict(row) for row in current.store.list_intents(character_id, limit=80)]
        return {
            "character_id": character_id,
            "now": current.clock.now().isoformat(),
            "provider": {
                "chat_model": current.settings.chat_model,
                "base_url": current.settings.base_url,
                "embedding_provider": current.settings.embedding_provider,
                "embedding_model": current.settings.embedding_model,
                "db_path": current.settings.db_path,
                "session_strategy": "conversation_id -> stable UUID",
            },
            "persona": current.runtime.persona,
            "mental_state": current.store.get_mental_state(character_id),
            "memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in reversed(memories)],
            "intents": intents,
        }

    @app.post("/v1/simulate")
    def simulate(req: SimulateRequest):
        current = require_bundle()
        return {"days": current.days.simulate(req.character_id, req.days)}

    @app.get("/v1/state/{character_id}")
    def state(character_id: str):
        current = require_bundle()
        return {
            "world_time": current.store.get_world_time(character_id),
            "mental_state": current.store.get_mental_state(character_id),
            "recent_events": [e.model_dump(mode="json") for e in current.store.list_events(character_id, 30)],
            "memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in current.store.list_memories(character_id, limit=30, include_embedding=False)],
            "intents": [dict(r) for r in current.store.list_intents(character_id)],
        }

    return app
