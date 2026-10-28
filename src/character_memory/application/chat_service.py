from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import inspect
import logging
import threading

from character_memory.application.clock import Clock
from character_memory.application.incoming_message import normalize_user_fact
from character_memory.domain.experience_scope import is_real_experience
from character_memory.domain.models import Event, EventType
from character_memory.message_projection import project_direct_message


logger = logging.getLogger("character_memory.application.chat")

REAL_EXPERIENCE_SCOPE = {"schema_version": 1, "reality": "REAL"}
_UNSET = object()


class ProactiveSourceScopeError(RuntimeError):
    """A proactive intent's authoritative source is not a real experience."""

    def __init__(self, reason: str, source_event_id: int | None = None):
        self.reason = reason
        self.source_event_id = source_event_id
        super().__init__(f"proactive intent source is not real: {reason}")


def _read_direct_source(store, source_event_id: int):
    """Read an intent's Direct source from whichever domain owns its id.

    A wake or intent may point at a conversation fact (unified table) or at a
    non-conversation prompt input such as TIME_TICK (legacy table). Using the
    reader here keeps a migrated user message from turning into SOURCE_MISSING.
    """
    reader = getattr(store, "fact_reader", None)
    if reader is not None:
        event = reader.get_canonical_event(source_event_id)
        if event is not None:
            return event
    return store.get_event(source_event_id)


def resolve_real_intent_source(store, source_event_id):
    """Refetch the intent's authoritative source and enforce the real boundary.

    Returns (event, "") when the source is an existing real fact, otherwise
    (None, reason). An absent source and a source relabelled SIMULATED or
    malformed both fail closed; callers must not synthesize a legacy REAL fact.
    """
    if source_event_id is None:
        return None, "SOURCE_MISSING"
    try:
        source = _read_direct_source(store, int(source_event_id))
    except (TypeError, ValueError):
        return None, "SOURCE_MISSING"
    if source is None:
        return None, "SOURCE_MISSING"
    if not is_real_experience(source.metadata):
        return None, "SOURCE_NOT_REAL"
    return source, ""


def _runtime_accepts_commit_guard(runtime) -> bool:
    try:
        return "commit_guard" in inspect.signature(runtime.handle).parameters
    except (TypeError, ValueError):
        return False


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
        recent = self.store.list_events(character_id, limit=32, before=at, real_experience_only=True)
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
                **({"experience_scope": item.metadata["experience_scope"]} if "experience_scope" in item.metadata else {}),
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
        stored = self.store.fact_writer.append_direct(
            event,
            owner_character_id=character_id,
            raw_conversation_id=conversation_id,
        )
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
        source_event_id=_UNSET,
    ):
        """Re-evaluate one due persisted intent as an ordinary Runtime event.

        The intent is only a pointer. Its authoritative source is refetched
        before any write and again inside Runtime's derived commit transaction:
        a source that disappeared, was relabelled SIMULATED or became malformed
        cannot produce real Memory/Intent/message output. The new event carries
        an explicit REAL scope rather than relying on legacy scope omission.
        """

        runtime = self._runtime_for(character_id)
        with self._lock_for(character_id):
            explicit_source = source_event_id is not _UNSET
            if not explicit_source:
                # Low-level callers may omit the argument; still use the stored
                # pointer when it exists. A missing intent row keeps the legacy
                # direct-call contract (tests and CLI call sites).
                row = self.store.conn.execute(
                    "SELECT source_event_id FROM intents WHERE id=? AND character_id=?",
                    (intent_id, character_id),
                ).fetchone()
                source_event_id = None if row is None else row["source_event_id"]
            elif source_event_id is not None:
                source_event_id = int(source_event_id)

            source = None
            if source_event_id is None:
                if explicit_source:
                    logger.warning(
                        "chat.proactive_scope_reject character=%s intent_id=%s reason=SOURCE_MISSING",
                        character_id,
                        intent_id,
                    )
                    raise ProactiveSourceScopeError("SOURCE_MISSING", None)
            else:
                source, scope_reason = resolve_real_intent_source(self.store, source_event_id)
                if source is None:
                    logger.warning(
                        "chat.proactive_scope_reject character=%s intent_id=%s source_event_id=%s reason=%s",
                        character_id,
                        intent_id,
                        source_event_id,
                        scope_reason,
                    )
                    raise ProactiveSourceScopeError(scope_reason, source_event_id)

            commit_guard = None
            if source is not None:
                guarded_source_id = int(source.id)
                source_character_id = source.character_id

                def commit_guard():
                    current = self.store.get_event(guarded_source_id)
                    if current is None or current.character_id != source_character_id:
                        raise ProactiveSourceScopeError("SOURCE_MISSING", guarded_source_id)
                    if not is_real_experience(current.metadata):
                        raise ProactiveSourceScopeError("SOURCE_CHANGED", guarded_source_id)
                    return True

            conversation_id = self._latest_conversation_id(character_id)
            self.store.set_world_time(character_id, at)
            logger.info(
                "chat.proactive character=%s intent_id=%s conversation=%s at=%s source_event_id=%s",
                character_id,
                intent_id,
                conversation_id,
                at.isoformat(),
                source_event_id if source_event_id is not None else "-",
            )
            event = Event(
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
                    "experience_scope": dict(REAL_EXPERIENCE_SCOPE),
                },
            )
            if commit_guard is not None and _runtime_accepts_commit_guard(runtime):
                return runtime.handle(event, commit_guard=commit_guard)
            return runtime.handle(event)

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
                        # A wake is an independent real-world tick with no intent
                        # source; state that boundary explicitly instead of
                        # relying on legacy scope omission.
                        "experience_scope": dict(REAL_EXPERIENCE_SCOPE),
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
