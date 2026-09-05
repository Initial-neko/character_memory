from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from character_memory.app import build_app
from character_memory.domain.models import Event, EventType


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    character_id: str = "rin"
    at: datetime | None = None


class SimulateRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=365)
    character_id: str = "rin"


def create_api(config_path: str = "config.yaml"):
    from fastapi import FastAPI

    bundle = build_app(config_path)
    app = FastAPI(title="character-memory", version="0.3.0")

    @app.on_event("shutdown")
    def _shutdown():
        bundle.store.close()

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "model": bundle.settings.chat_model,
            "embedding_provider": bundle.settings.embedding_provider,
            "db_path": bundle.settings.db_path,
        }

    @app.post("/v1/chat")
    def chat(req: ChatRequest):
        now = req.at or bundle.days.current_time(req.character_id)
        out = bundle.runtime.handle(
            Event(character_id=req.character_id, event_type=EventType.USER_MESSAGE, event_time=now, content=req.message)
        )
        bundle.store.set_world_time(req.character_id, now + timedelta(minutes=1))
        return {
            "action": out.reaction.action.model_dump(mode="json"),
            "perception": out.reaction.perception,
            "reaction": out.reaction.reaction,
            "mental_state": out.reaction.mental_state_update,
            "recalled_memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in out.recalled_memories],
        }

    @app.post("/v1/simulate")
    def simulate(req: SimulateRequest):
        return {"days": bundle.days.simulate(req.character_id, req.days)}

    @app.get("/v1/state/{character_id}")
    def state(character_id: str):
        return {
            "world_time": bundle.store.get_world_time(character_id),
            "mental_state": bundle.store.get_mental_state(character_id),
            "recent_events": [e.model_dump(mode="json") for e in bundle.store.list_events(character_id, 30)],
            "memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in bundle.store.list_memories(character_id)[-30:]],
            "intents": [dict(r) for r in bundle.store.list_intents(character_id)],
        }

    return app
