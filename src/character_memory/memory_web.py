from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from pydantic import BaseModel, Field

from character_memory.domain.models import Memory


class MemoryActiveRequest(BaseModel):
    active: bool


class MemoryPinRequest(BaseModel):
    pinned: bool


class MemoryCorrectionRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


def _ensure_character(access, character_id: str) -> None:
    if not any(item["id"] == character_id for item in access.character_profiles()):
        raise HTTPException(status_code=404, detail=f"Unknown character: {character_id}")


def _memory_payload(store, memory: Memory) -> dict:
    source = None
    metadata = dict(memory.metadata or {})
    origin = str(metadata.get("origin") or "").upper()

    if origin == "GROUP":
        source = {
            "kind": "GROUP",
            "conversation_id": metadata.get("conversation_id"),
            "event_id": metadata.get("source_conversation_event_id"),
            "turn_id": metadata.get("turn_id"),
        }
    elif memory.source_event_id is not None:
        event = store.get_event(memory.source_event_id)
        if event is not None:
            source = {
                "kind": str(event.metadata.get("channel") or "DIRECT"),
                "event_id": event.id,
                "event_type": event.event_type.value,
                "event_time": event.event_time.isoformat(),
                "content": event.content,
                "metadata": event.metadata,
            }

    return {
        **memory.model_dump(mode="json", exclude={"embedding"}),
        "source": source,
    }


def attach_memory_routes(app) -> None:
    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before memory routes are attached")

    @app.get("/v1/characters/{character_id}/memories")
    def list_memories(character_id: str, include_inactive: bool = True, limit: int = 80):
        _ensure_character(access, character_id)
        store = access.store()
        items = store.list_memories(
            character_id,
            include_inactive=include_inactive,
            limit=max(1, min(int(limit), 300)),
            include_embedding=False,
        )
        return {
            "character_id": character_id,
            "memories": [_memory_payload(store, item) for item in reversed(items)],
        }

    @app.post("/v1/characters/{character_id}/memories/{memory_id}/active")
    def set_memory_active(character_id: str, memory_id: int, req: MemoryActiveRequest):
        _ensure_character(access, character_id)
        store = access.store()
        existing = store.get_memory(memory_id)
        if existing is None or existing.character_id != character_id:
            raise HTTPException(status_code=404, detail="memory not found")
        updated = store.set_memory_active(memory_id, req.active)
        return {"memory": _memory_payload(store, updated)}

    @app.post("/v1/characters/{character_id}/memories/{memory_id}/pin")
    def set_memory_pin(character_id: str, memory_id: int, req: MemoryPinRequest):
        _ensure_character(access, character_id)
        store = access.store()
        existing = store.get_memory(memory_id)
        if existing is None or existing.character_id != character_id:
            raise HTTPException(status_code=404, detail="memory not found")
        updated = store.set_memory_pinned(memory_id, req.pinned)
        return {"memory": _memory_payload(store, updated)}

    @app.post("/v1/characters/{character_id}/memories/{memory_id}/correct")
    def correct_memory(character_id: str, memory_id: int, req: MemoryCorrectionRequest):
        _ensure_character(access, character_id)
        store = access.store()
        existing = store.get_memory(memory_id)
        if existing is None or existing.character_id != character_id:
            raise HTTPException(status_code=404, detail="memory not found")

        content = " ".join(req.content.split()).strip()
        if not content:
            raise HTTPException(status_code=400, detail="corrected memory must not be empty")
        if content.casefold() == existing.content.strip().casefold():
            raise HTTPException(status_code=400, detail="corrected memory is unchanged")

        try:
            embedding = access.require_bundle().embeddings.embed(content)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"memory embedding unavailable: {exc}") from exc

        metadata = dict(existing.metadata or {})
        metadata.update(
            {
                "governance": "CORRECTED",
                "corrects_memory_id": existing.id,
                "corrected_at": datetime.now().astimezone().isoformat(),
            }
        )
        replacement = Memory(
            character_id=character_id,
            content=content,
            memory_type=existing.memory_type,
            event_time=datetime.now().astimezone(),
            importance=max(float(existing.importance), 0.7),
            source_event_id=existing.source_event_id,
            active=True,
            pinned=existing.pinned,
            metadata=metadata,
            embedding=embedding,
        )
        result = store.supersede_memory(memory_id, replacement)
        if result is None:
            raise HTTPException(status_code=404, detail="memory not found")
        old, saved = result
        return {
            "old_memory": _memory_payload(store, old),
            "memory": _memory_payload(store, saved),
        }
