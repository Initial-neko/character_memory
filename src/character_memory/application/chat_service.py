from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import logging
import threading

from character_memory.application.clock import Clock
from character_memory.application.incoming_message import normalize_user_fact
from character_memory.domain.models import Event, EventType
from character_memory.message_projection import project_direct_message


logger = logging.getLogger("character_memory.application.chat")


def build_user_event(
    message: str,
    *,
    character_id: str,
    conversation_id: str,
    at: datetime,
    sticker: dict | None = None,
    image: dict | None = None,
) -> Event:
    """Build one immutable Direct user fact without running the character model."""

    normalized = normalize_user_fact(message, sticker=sticker, image=image)
    return Event(
        character_id=character_id,
        event_type=EventType.USER_MESSAGE,
        event_time=at,
        content=normalized.runtime_content,
        metadata={
            **normalized.metadata,
            "channel": "DIRECT",
            "conversation_id": conversation_id,
        },
    )


class ChatService:
    """Application entry point for direct conversation state.

    `persist_user_message` is deliberately independent from model generation so
    asynchronous Web/API callers can durably accept user input immediately. The
    legacy `send` method intentionally keeps the long-standing synchronous
    `runtime.handle(event)` contract for CLI callers and lightweight test runtimes.
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

    def _runtime_for(self, character_id: str):
        if isinstance(self.runtime, dict):
            selected = self.runtime.get(character_id)
            if selected is None:
                known = ", ".join(sorted(self.runtime))
                raise KeyError(f"unknown character_id={character_id!r}; known={known}")
            return selected
        return self.runtime

    def _latest_conversation_id(self, character_id: str) -> str:
        events = self.store.list_chat_events(character_id, limit=1)
        if events:
            value = str(events[-1].metadata.get("conversation_id") or "").strip()
            if value:
                return value
        return f"{character_id}:proactive"

    def _sharing_observations(self, character_id: str, at: datetime) -> list[dict]:
        """Small read-only window of sourced observations, not unread inventory.

        Keep original reading scope/counts: a feed excerpt is never upgraded to
        a fetched original. The snapshot belongs to this opportunity's trace.
        """
        recent = self.store.list_events(character_id, limit=32, before=at)
        candidates = []
        for item in reversed(recent):
            channel = item.metadata.get("channel")
            sources = item.metadata.get("sources")
            if (item.event_type != EventType.WORLD_OBSERVATION
                    or channel not in {"PERSONAL_BROWSE", "WORLD_PULSE"}
                    or not isinstance(sources, list)):
                continue
            sources = [value[:240] for value in sources if isinstance(value, str) and value.strip()][:2]
            if not sources:
                continue
            reading = item.metadata.get("rss_reading")
            reading = reading if isinstance(reading, dict) else {}
            reading = {key: value for key, value in reading.items()
                       if key in {"title", "content_scope", "truncated", "read_characters", "available_characters"}
                       and isinstance(value, (str, int, bool))}
            reading = {key: value[:180] if isinstance(value, str) else value for key, value in reading.items()}
            candidates.append({
                "event_id": item.id,
                "observed_at": item.event_time.isoformat(),
                "channel": channel,
                "content": item.content[:600],
                "context_excerpt": len(item.content) > 600,
                "sources": sources,
                "rss_reading": reading,
            })
            if len(candidates) == 4:
                break
        return candidates

    def persist_user_message(
        self,
        message: str,
        *,
        character_id: str = "rin",
        conversation_id: str = "default",
        at: datetime | None = None,
        sticker: dict | None = None,
        image: dict | None = None,
    ) -> Event:
        now = at or self.clock.now()
        event = build_user_event(
            message,
            character_id=character_id,
            conversation_id=conversation_id,
            at=now,
            sticker=sticker,
            image=image,
        )
        stored = self.store.append_event(event)
        logger.info(
            "chat.persist character=%s conversation=%s event_id=%s at=%s chars=%d sticker=%s image=%s",
            character_id,
            conversation_id,
            stored.id,
            now.isoformat(),
            len(message.strip()),
            stored.metadata.get("sticker_id") or "-",
            stored.metadata.get("media_id") or "-",
        )
        return stored

    def send(
        self,
        message: str,
        *,
        character_id: str = "rin",
        conversation_id: str = "default",
        at: datetime | None = None,
        sticker: dict | None = None,
        image: dict | None = None,
        vision_image_data_url: str | None = None,
    ):
        runtime = self._runtime_for(character_id)
        with self._lock_for(character_id):
            now = at or self.clock.now()
            self.store.set_world_time(character_id, now)
            event = build_user_event(
                message,
                character_id=character_id,
                conversation_id=conversation_id,
                at=now,
                sticker=sticker,
                image=image,
            )
            logger.info(
                "chat.send character=%s conversation=%s at=%s chars=%d sticker=%s image=%s",
                character_id,
                conversation_id,
                now.isoformat(),
                len(message.strip()),
                event.metadata.get("sticker_id") or "-",
                event.metadata.get("media_id") or "-",
            )
            if vision_image_data_url:
                return runtime.handle(event, image_data_urls=[vision_image_data_url])
            return runtime.handle(event)

    def dispatch_proactive_intent(
        self,
        *,
        character_id: str,
        intent_id: int,
        content: str,
        at: datetime,
        intent_deferral_policy: dict | None = None,
    ):
        """Re-evaluate one due persisted intent as an ordinary Runtime event."""

        runtime = self._runtime_for(character_id)
        with self._lock_for(character_id):
            conversation_id = self._latest_conversation_id(character_id)
            self.store.set_world_time(character_id, at)
            logger.info(
                "chat.proactive character=%s intent_id=%s conversation=%s at=%s",
                character_id,
                intent_id,
                conversation_id,
                at.isoformat(),
            )
            return runtime.handle(
                Event(
                    character_id=character_id,
                    event_type=EventType.PROACTIVE_INTENT,
                    event_time=at,
                    content=f"之前留下的意图：{content}。现在重新判断是否自然执行、保持沉默或放弃。",
                    metadata={
                        "intent_id": intent_id,
                        "sharing_observations": self._sharing_observations(character_id, at),
                        "channel": "DIRECT",
                        "intent_deferral_policy": intent_deferral_policy,
                        "conversation_id": conversation_id,
                    },
                )
            )

    def dispatch_wake(
        self,
        *,
        character_id: str,
        at: datetime,
        reason: str = "PERIODIC",
        conversation_id: str | None = None,
    ):
        """Give one direct character a TIME_TICK opportunity to think.

        A wake is not an instruction to speak. Runtime keeps the ordinary
        TIME_TICK contract, so MESSAGE/STICKER/IMAGE, future Intent, or silence
        are all valid outcomes.
        """

        normalized_reason = str(reason or "PERIODIC").strip().upper()
        if normalized_reason != "PERIODIC":
            raise ValueError(f"unknown wake reason: {reason}")
        runtime = self._runtime_for(character_id)
        with self._lock_for(character_id):
            resolved_conversation = str(conversation_id or "").strip() or self._latest_conversation_id(character_id)
            self.store.set_world_time(character_id, at)
            content = (
                "时间自然过去了一段。结合最近聊天、Memory、Mental State 和 Persona，"
                "判断此刻是否真的有想主动表达或想留到未来的念头；不要为了活跃而强行说话。"
            )
            logger.info(
                "chat.wake character=%s reason=%s conversation=%s at=%s",
                character_id,
                normalized_reason,
                resolved_conversation,
                at.isoformat(),
            )
            return runtime.handle(
                Event(
                    character_id=character_id,
                    event_type=EventType.TIME_TICK,
                    event_time=at,
                    content=content,
                    metadata={
                        "wake_reason": normalized_reason,
                        "sharing_observations": self._sharing_observations(character_id, at),
                        "channel": "DIRECT",
                        "conversation_id": resolved_conversation,
                    },
                )
            )

    def history(self, character_id: str = "rin", limit: int = 160) -> dict:
        events = self.store.list_chat_events(character_id, limit=limit)
        trace_sources = self.store.list_runtime_trace_sources(character_id)

        messages = []
        for event in events:
            source_event_id = (
                event.id
                if event.event_type == EventType.USER_MESSAGE
                else event.metadata.get("source_event_id")
            )
            messages.append(
                project_direct_message(
                    event,
                    has_trace=source_event_id in trace_sources,
                    include_legacy_labels=True,
                )
            )

        return {"character_id": character_id, "messages": messages}
