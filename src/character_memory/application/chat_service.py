from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import logging
import threading

from character_memory.application.clock import Clock
from character_memory.domain.models import Event, EventType


logger = logging.getLogger("character_memory.application.chat")


class ChatService:
    """Single application entry point for one conversational turn.

    UI/API/CLI should call this service instead of deciding clock/session/runtime
    semantics on their own.
    """

    def __init__(self, store, runtime, clock: Clock):
        self.store = store
        self.runtime = runtime
        self.clock = clock
        self._locks_guard = threading.Lock()
        self._character_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    def _lock_for(self, character_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._character_locks[character_id]

    def send(
        self,
        message: str,
        *,
        character_id: str = "rin",
        conversation_id: str = "default",
        at: datetime | None = None,
    ):
        content = message.strip()
        if not content:
            raise ValueError("message must not be empty")

        with self._lock_for(character_id):
            now = at or self.clock.now()
            self.store.set_world_time(character_id, now)
            logger.info(
                "chat.send character=%s conversation=%s at=%s chars=%d",
                character_id,
                conversation_id,
                now.isoformat(),
                len(content),
            )
            result = self.runtime.handle(
                Event(
                    character_id=character_id,
                    event_type=EventType.USER_MESSAGE,
                    event_time=now,
                    content=content,
                    metadata={"conversation_id": conversation_id},
                )
            )
            return result

    def history(self, character_id: str = "rin", limit: int = 160) -> dict:
        events = self.store.list_chat_events(character_id, limit=limit)
        trace_sources = self.store.list_runtime_trace_sources(character_id)

        messages = []
        for event in events:
            if event.event_type == EventType.USER_MESSAGE:
                role = "user"
                source_event_id = event.id
            else:
                role = "assistant"
                source_event_id = event.metadata.get("source_event_id")

            messages.append(
                {
                    "id": event.id,
                    "role": role,
                    "content": event.content,
                    "event_time": event.event_time.isoformat(),
                    "action": event.metadata.get("action"),
                    "source_event_id": source_event_id,
                    "has_trace": source_event_id in trace_sources,
                }
            )

        return {"character_id": character_id, "messages": messages}
