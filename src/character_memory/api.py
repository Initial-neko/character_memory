from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from character_memory.app import AppBundle, build_app
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
    app_bundle = bundle or build_app(config_path)
    app = FastAPI(title="character-memory", version="0.4.1")

    web_dir = Path(__file__).with_name("web")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.on_event("shutdown")
    def _shutdown():
        if own_bundle:
            app_bundle.close()

    @app.get("/")
    def web_index():
        return FileResponse(web_dir / "index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "model": app_bundle.settings.chat_model, "embedding_provider": app_bundle.settings.embedding_provider, "db_path": app_bundle.settings.db_path}

    @app.get("/v1/chat/history")
    def history(character_id: str = "rin", limit: int = 160):
        limit = max(1, min(limit, 500))
        return app_bundle.chat.history(character_id, limit)

    @app.post("/v1/chat")
    def chat(req: ChatRequest):
        logger.info("api.chat character=%s conversation=%s chars=%d", req.character_id, req.conversation_id, len(req.message))
        out = app_bundle.chat.send(req.message, character_id=req.character_id, conversation_id=req.conversation_id, at=req.at)
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
        value = app_bundle.store.get_runtime_trace(source_event_id)
        if value is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return value

    @app.get("/v1/runtime/{character_id}")
    def runtime_state(character_id: str):
        memories = app_bundle.store.list_memories(character_id, include_inactive=True, limit=80, include_embedding=False)
        intents = [dict(row) for row in app_bundle.store.list_intents(character_id, limit=80)]
        return {
            "character_id": character_id,
            "now": app_bundle.clock.now().isoformat(),
            "provider": {
                "chat_model": app_bundle.settings.chat_model,
                "base_url": app_bundle.settings.base_url,
                "embedding_provider": app_bundle.settings.embedding_provider,
                "embedding_model": app_bundle.settings.embedding_model,
                "db_path": app_bundle.settings.db_path,
                "session_strategy": "conversation_id -> stable UUID",
            },
            "persona": app_bundle.runtime.persona,
            "mental_state": app_bundle.store.get_mental_state(character_id),
            "memories": [memory.model_dump(mode="json", exclude={"embedding"}) for memory in reversed(memories)],
            "intents": intents,
        }

    @app.post("/v1/simulate")
    def simulate(req: SimulateRequest):
        return {"days": app_bundle.days.simulate(req.character_id, req.days)}

    @app.get("/v1/state/{character_id}")
    def state(character_id: str):
        return {
            "world_time": app_bundle.store.get_world_time(character_id),
            "mental_state": app_bundle.store.get_mental_state(character_id),
            "recent_events": [e.model_dump(mode="json") for e in app_bundle.store.list_events(character_id, 30)],
            "memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in app_bundle.store.list_memories(character_id, limit=30, include_embedding=False)],
            "intents": [dict(r) for r in app_bundle.store.list_intents(character_id)],
        }

    return app
