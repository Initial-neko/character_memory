from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import logging
import threading

from character_memory.application.clock import Clock
from character_memory.domain.models import Event, EventType
from character_memory.voice_message_fields import voice_fields


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
    """Build one immutable user fact without running the character model."""
    content = message.strip()
    if not content and sticker is None and image is None:
        raise ValueError("message, sticker or image must not be empty")

    metadata = {"conversation_id": conversation_id}
    runtime_parts = [content] if content else []

    if sticker is not None:
        sticker_id = str(sticker.get("id") or "").strip()
        sticker_label = str(sticker.get("label") or sticker_id).strip()
        tags = [str(value).strip() for value in (sticker.get("tags") or []) if str(value).strip()]
        description = str(sticker.get("description") or "").strip()
        meaning = "、".join(tags) or description or sticker_label
        runtime_parts.append(f"[用户发送表情包：{sticker_label}；含义：{meaning}]")
        metadata.update(
            {
                "display_text": content,
                "sticker_id": sticker_id,
                "sticker_label": sticker_label,
            }
        )

    if image is not None:
        media_id = str(image.get("id") or "").strip()
        original_name = str(image.get("original_name") or "图片").strip() or "图片"
        mime_type = str(image.get("mime_type") or "").strip()
        size_bytes = int(image.get("size_bytes") or 0)
        runtime_parts.append(f"[用户发送真实图片：{original_name}。图片本体已随本轮多模态请求提供，请根据实际视觉内容理解。]")
        metadata.update(
            {
                "display_text": content,
                "media_id": media_id,
                "media_name": original_name,
                "media_mime_type": mime_type,
                "media_size_bytes": size_bytes,
            }
        )

    return Event(
        character_id=character_id,
        event_type=EventType.USER_MESSAGE,
        event_time=at,
        content="\n".join(runtime_parts).strip(),
        metadata=metadata,
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

        runtime = self._runtime_for(character_id)
        normalized_reason = str(reason or "PERIODIC").strip().upper()
        with self._lock_for(character_id):
            resolved_conversation = str(conversation_id or "").strip() or self._latest_conversation_id(character_id)
            self.store.set_world_time(character_id, at)
            if normalized_reason == "MANUAL":
                content = (
                    "现在进行一次手动唤醒后的主动判断。结合最近聊天、Memory、Mental State 和 Persona，"
                    "看看此刻是否真的有想表达或想留到未来的念头；这不是要求你必须回复。"
                )
            else:
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
                        "conversation_id": resolved_conversation,
                    },
                )
            )

    def history(self, character_id: str = "rin", limit: int = 160) -> dict:
        events = self.store.list_chat_events(character_id, limit=limit)
        trace_sources = self.store.list_runtime_trace_sources(character_id)

        messages = []
        for event in events:
            if event.event_type == EventType.USER_MESSAGE:
                role = "user"
                source_event_id = event.id
                content = event.metadata.get("display_text", event.content)
            else:
                role = "assistant"
                source_event_id = event.metadata.get("source_event_id")
                content = event.content

            messages.append(
                {
                    "id": event.id,
                    "role": role,
                    "content": content,
                    "event_time": event.event_time.isoformat(),
                    "action": event.metadata.get("action"),
                    "sticker_id": event.metadata.get("sticker_id"),
                    "sticker_label": event.metadata.get("sticker_label"),
                    "image_id": event.metadata.get("image_id"),
                    "image_label": event.metadata.get("image_label"),
                    "media_id": event.metadata.get("media_id"),
                    "media_name": event.metadata.get("media_name"),
                    # Deliberately distinct keys from "media_id"/"media_name"
                    # above, which drive image rendering. None for every
                    # non-voice message.
                    **voice_fields(event.metadata),
                    "source_event_type": event.metadata.get("source_event_type"),
                    "source_event_id": source_event_id,
                    "has_trace": source_event_id in trace_sources,
                }
            )

        return {"character_id": character_id, "messages": messages}
