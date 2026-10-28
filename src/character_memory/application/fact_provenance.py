"""Read-only provenance for legacy Memory references, without schema creation."""
from __future__ import annotations

from character_memory.domain.facts import FactRef
from character_memory.domain.models import Memory
from character_memory.group_store import GroupRepository


def resolve_memory_provenance(store, memory: Memory) -> dict:
    metadata = memory.metadata or {}
    grouped = str(metadata.get("origin") or "").upper() == "GROUP"
    try:
        if grouped:
            ref = FactRef(kind="CONVERSATION_EVENT",
                id=metadata.get("source_conversation_event_id"),
                conversation_id=metadata.get("conversation_id"))
            turn_id = metadata.get("turn_id")
            if turn_id is not None and (not isinstance(turn_id, str) or not turn_id.strip()):
                raise ValueError("invalid group turn reference")
        elif "source_conversation_event_id" in metadata:
            raise ValueError("group reference lacks its domain discriminator")
        elif memory.source_event_id is not None:
            ref = FactRef(kind="EVENT", id=memory.source_event_id)
        else:
            return {"ref": None, "status": "UNRECORDED", "fact": None}
    except ValueError:
        return {"ref": None, "status": "INVALID", "fact": None}

    result = {"ref": ref.model_dump(), "status": "UNRESOLVED", "fact": None}
    try:
        if ref.kind == "EVENT":
            # Direct facts may be a preserved legacy row or a unified
            # conversation fact; the reader owns that distinction.
            event = store.fact_reader.resolve_direct(ref, character_id=memory.character_id)
            if event is None:
                return result
        else:
            event = GroupRepository.read_source_fact(store, ref.conversation_id,
                ref.id, memory.character_id, turn_id=turn_id)
            if event is None:
                return result
    except (ValueError, TypeError):
        # Bad historical timestamps/JSON/schema values do not break inspection.
        # Database/operational errors still propagate; they are not missing facts.
        return result
    return {**result, "status": "RESOLVED", "fact": {**event.model_dump(mode="json"), "event_time": event.event_time.isoformat()}}
